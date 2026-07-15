"""Inter-section edge routing: bypass, entry wraps, around-section,
inter-row corridors, stepped descent, and L-shape handlers.

``_route_inter_section`` selects the shape via a declarative table
(``_INTER_SECTION_RULES``): one :class:`_InterFacts` snapshot of the edge's
geometry and topology is matched against an ordered list of named rules, and
the first whose predicate holds owns the route.  The rule order is the
combinatorial space documented in ``docs/dev/inter_section_dispatch.mdx``.
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
    route_offset,
    route_straight,
    route_tapered,
    route_tapered_anchored,
)
from nf_metro.layout.routing.common import (
    Direction,
    RoutedPath,
    _center_inter_row_channel,
    _inter_row_band_fits,
    bundle_width,
    bypass_bottom_y,
    clear_channel_of_section_edge,
    col_left_edge,
    col_right_edge,
    column_gap_edges,
    column_gap_midpoint,
    endpoint_port_xs,
    gap_lo_for_x,
    header_corridor_y,
    horizontal_direction,
    inter_column_channel_x,
    inter_row_channel_y,
    inter_row_gap_upper_row,
    inter_row_wrap_band,
    iter_horizontal_trunks,
    iter_vertical_segments,
    max_grid_row_with_content,
    merge_trunk_force_cross_row,
    needs_perp_approach_fan,
    resolve_section,
    row_bottom_edge,
    row_top_edge,
    section_header_safe_cap,
    symmetric_bundle_midpoint,
    trailing_perp_side,
    vertical_direction,
)
from nf_metro.layout.routing.context import (
    _get_offset,
    _has_intervening_sections,
    _resolve_section_col,
    _resolve_section_colrow,
    _resolve_section_row,
    _RoutingCtx,
    _tb_x_offset,
    is_near_vertical_drop,
)
from nf_metro.layout.routing.corners import (
    bypass_stagger,
    l_shape_stagger,
)
from nf_metro.layout.routing.normalize import (
    _clear_channel_x_in_band,
    _gap_channel_base,
    _h_segment_crosses_other_section,
    _v_segment_crosses_other_section,
)
from nf_metro.layout.routing.perp import (
    _perp_approach_fan_x,
    _perp_entry_crossing_x,
    _perp_riser_lateral,
)
from nf_metro.parser.model import (
    Edge,
    MetroGraph,
    Port,
    PortSide,
    Section,
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
        """Source is the trailing perp exit on a vertical-flow (TB/BT) section.

        The trunk continues out the section's trailing TOP/BOTTOM edge -- BOTTOM
        for a downward (TB) flow, TOP for its upward (BT) image -- so the drop
        rides the section's own rotation lane out of that port.
        """
        if not (
            self.src_port is not None
            and not self.src_port.is_entry
            and self.src.section_id in self.ctx.tb_sections
            and bool(self.ctx.station_offsets)
        ):
            return False
        section = self.graph.sections.get(self.src.section_id)
        return section is not None and self.src_port.side == trailing_perp_side(
            section.direction
        )

    @property
    def tb_bottom_exit_drops_through_stack(self) -> bool:
        """A TB bottom-exit straight drop would plough an intervening section.

        The flow-direction drop (:func:`_route_tb_bottom_exit`) descends the
        exit column straight to the target.  When other sections are stacked in
        that column between the source and the target -- a convergence sink
        folded below its branches, fed through a TOP entry -- the drop crosses
        their boxes away from any port.  Such a feeder diverts through a clear
        inter-column gap instead (:func:`_route_tb_bottom_exit_around_stack`).
        """
        if not self.is_tb_bottom_exit:
            return False
        exclude = {
            sid for sid in (self.src.section_id, self.tgt.section_id) if sid is not None
        }
        return _v_segment_crosses_other_section(
            self.graph, self.sx, self.sy, self.ty, exclude
        )

    @property
    def is_tb_perp_exit_against_flow(self) -> bool:
        """A trailing perp exit on a vertical-flow section feeding an entry the
        flow-direction drop can't reach.

        The exit port sits on the section's trailing edge (BOTTOM for a downward
        TB flow, TOP for its upward BT image), so the line leaves along the flow.
        Any entry sitting *against* the flow from the port -- at or above a
        downward exit, at or below an upward one -- cannot be reached by that
        drop: a straight or shallow run grazes the trailing edge and exits
        through the corner, and a side or perpendicular entry on the far side of
        the target would be reached only by clawing back up through the box.
        Such an edge takes the up/down-and-over corridor route instead (see
        _route_perp_exit_over, whose ``crosses_box`` branch crosses to the
        inter-column gap and approaches the port from outside), mirroring how
        :attr:`is_perp_exit` intercepts horizontal-flow perpendicular exits
        before the same-Y shortcut.
        """
        if not (
            self.src_port is not None
            and not self.src_port.is_entry
            and self.src.section_id in self.ctx.tb_sections
            and self.entry_side is not None
        ):
            return False
        section = self.graph.sections.get(self.src.section_id)
        if section is None or self.src_port.side != trailing_perp_side(
            section.direction
        ):
            return False
        if self.src_port.side == PortSide.BOTTOM:
            return self.ty <= self.sy + COORD_TOLERANCE
        return self.ty >= self.sy - COORD_TOLERANCE

    @property
    def right_entry_from_left(self) -> bool:
        """Target is a RIGHT entry port whose source sits to its left.

        A straight or interior-cutting approach would plough through the box to
        reach the far-edge port, so such an edge wraps in from the port's own
        outward side instead.
        """
        return self.entry_side is PortSide.RIGHT and self.sx < self.tx - COORD_TOLERANCE

    @property
    def left_entry_from_right(self) -> bool:
        """Target is a LEFT entry port whose source sits to its right.

        The mirror of :attr:`right_entry_from_left`.  A U-shaped bypass would
        rise in the gap to the RIGHT of the target and run its final horizontal
        LEFTWARD across the section interior to reach the far-edge (left) port;
        instead such an edge wraps around below to enter from the port's own
        outward side.
        """
        return self.entry_side is PortSide.LEFT and self.sx > self.tx + COORD_TOLERANCE

    @property
    def is_perp_exit_farside_entry_wrap(self) -> bool:
        """A trailing perp (BOTTOM/TOP) exit feeding a LEFT/RIGHT entry on the
        target's *far* side, reached by wrapping through the inter-row gap.

        The trunk leaves a vertical-flow section along the flow, out its trailing
        TOP/BOTTOM edge; the consumer is a LEFT/RIGHT entry whose port faces away
        from the source (:attr:`left_entry_from_right` / its right-side mirror).
        The flow-direction drop (:func:`_route_tb_bottom_exit`) would run down the
        target's own border to reach the far-edge port, and the LEFT/RIGHT-entry
        wrap family's sideways lead-out would claw back across the source box; the
        clean shape leaves the port along the flow into the gap, then wraps around
        the target to approach the port horizontally.

        Unlike :attr:`is_tb_bottom_exit` this is offset-independent, so the
        validate and render routing paths dispatch it identically.
        """
        if not (
            self.src_port is not None
            and not self.src_port.is_entry
            and self.src.section_id in self.ctx.tb_sections
        ):
            return False
        section = self.graph.sections.get(self.src.section_id)
        if section is None or self.src_port.side != trailing_perp_side(
            section.direction
        ):
            return False
        return self.cross_row and (
            self.left_entry_from_right or self.right_entry_from_left
        )

    @property
    def is_merge_trunk(self) -> bool:
        """Source carries the full bypass trunk of its merge junction."""
        return self.ctx.merge.trunk_source.get(self.edge.target) == self.edge.source

    @property
    def is_merge_branch(self) -> bool:
        """Source is a non-trunk feeder of a merge junction that has a trunk.

        Every feeder of a merge with a trunk joins the trunk's bypass channel
        as a branch so the converging line stays a single stroke; only the
        trunk carries the full route to the entry port.  A feeder that does not
        individually need a bypass would otherwise route straight into the entry
        on its own lateral slot and draw as a second parallel stroke.
        """
        trunk = self.ctx.merge.trunk_source.get(self.edge.target)
        return trunk is not None and trunk != self.edge.source

    @property
    def is_near_vertical_same_col_junction(self) -> bool:
        """Junction dropping almost straight into a same-column entry."""
        return (
            self.edge.source in self.ctx.junction_ids
            and is_near_vertical_drop(self.dx, self.dy)
            and self.same_col
        )

    @property
    def takes_near_vertical_junction_drop(self) -> bool:
        """A near-vertical junction drop the straight-drop handler can nest.

        The drop leads its channel out to the junction's outward side; a RIGHT
        entry must be reached from ITS outward side, so a multi-line bundle would
        hook back through opposite-handed corners it cannot nest.  Such a target
        cedes to the cross-row wrap rule, which drops down the port's outward side
        and turns in once; everything else drops straight.
        """
        return self.is_near_vertical_same_col_junction and not (
            self.entry_side is PortSide.RIGHT and self.n >= 2
        )

    @property
    def is_left_exit(self) -> bool:
        """Source is a LEFT-side exit port (not an entry)."""
        return (
            self.src_port is not None
            and not self.src_port.is_entry
            and self.src_port.side is PortSide.LEFT
        )

    @property
    def is_serpentine_left_exit_left_entry(self) -> bool:
        """LEFT exit dropping into a LEFT entry stacked in the same column."""
        return (
            self.is_left_exit
            and self.entry_side is PortSide.LEFT
            and self.same_col
            and self.cross_row
        )


def _packed_cell_mate_obstructs(
    graph: MetroGraph,
    src: Station,
    tgt: Station,
    src_row: int | None,
    tgt_row: int | None,
) -> bool:
    """Whether a same-row section other than *src*'s/*tgt*'s own sits on the
    straight path between them.

    ``_has_intervening_sections`` only sees columns strictly between the
    endpoints' grid columns. A packed cell (``%%metro grid: a, b | col,row``)
    can place more than one section in a boundary column itself, so a
    cell-mate of the route's own endpoint can sit geometrically between the
    two ports without ever showing up as an "intervening" column.
    """
    if src_row is None or tgt_row is None or src_row != tgt_row:
        return False
    src_sec = resolve_section(graph, src, prefer_upstream=False)
    tgt_sec = resolve_section(graph, tgt, prefer_upstream=False)
    exclude = {sec.id for sec in (src_sec, tgt_sec) if sec is not None}
    return _h_segment_crosses_other_section(graph, src.x, tgt.x, src.y, exclude)


def _intervening_section_obstructs(
    graph: MetroGraph,
    src_col: int,
    src_row: int | None,
    tgt_col: int,
    tgt_row: int | None,
) -> bool:
    """Whether a multi-column hop is blocked by a section in a column it spans.

    The horizontal run blocks on the source row, or - for a cross-row L-shape,
    whose horizontal leg runs at the target entry Y - the target row, plowed
    through even when the source row is clear. Only meaningful when the columns
    are more than one apart; an adjacent hop has no column between them to
    intervene.
    """
    if abs(tgt_col - src_col) <= 1:
        return False
    if _has_intervening_sections(graph, src_col, tgt_col, src_row):
        return True
    return (
        src_row is not None
        and tgt_row is not None
        and tgt_row != src_row
        and _has_intervening_sections(graph, src_col, tgt_col, tgt_row)
    )


def _build_inter_facts(
    edge: Edge, src: Station, tgt: Station, ctx: _RoutingCtx
) -> _InterFacts:
    graph = ctx.graph
    src_col, src_row = _resolve_section_colrow(graph, src)
    tgt_col, tgt_row = _resolve_section_colrow(graph, tgt)
    # Two independent triggers force a bypass: a multi-column hop blocked by an
    # intervening section, or a packed cell-mate of either endpoint sitting on
    # the straight path. The latter is independent of the column gap - a same-row
    # cell-mate can obstruct even an adjacent-column hop, where it never registers
    # as an intervening column (see _packed_cell_mate_obstructs).
    needs_bypass = (
        src_col is not None
        and tgt_col is not None
        and (
            _intervening_section_obstructs(graph, src_col, src_row, tgt_col, tgt_row)
            or _packed_cell_mate_obstructs(graph, src, tgt, src_row, tgt_row)
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
    route = route_hvh_tapered(
        ctx, f.edge, f.src, f.tgt, channel_x, base_radius=ctx.curve_radius
    )
    _declare_channel(route, ctx, channel_x, vertical_direction(f.ty - f.sy))
    return route


def _route_merge_trunk_feeder(f: _InterFacts) -> RoutedPath | None:
    """Dispatch wrapper: the trunk feeder's full bypass to the entry port."""
    assert f.src_col is not None and f.tgt_col is not None
    return _route_merge_trunk(
        f.edge, f.src, f.tgt, f.i, f.n, f.src_col, f.tgt_col, f.ctx, f.src_row
    )


def _route_merge_branch_feeder(f: _InterFacts) -> RoutedPath | None:
    """Dispatch wrapper: a non-trunk feeder's descent onto the trunk channel."""
    assert f.src_col is not None
    return _route_merge_branch(f.edge, f.src, f.ctx, f.src_col)


def _route_bypass_family(f: _InterFacts) -> RoutedPath | None:
    """Multi-column hop past intervening sections (``needs_bypass``).

    A LEFT entry one row directly below drops straight in when the entry-Y
    horizontal is clear (no canvas-bottom loop); a RIGHT entry fed from the left
    wraps around its outward side (via the inter-row gap above when clear, else
    the around-below loop); a far-side LEFT entry fed from a LEFT exit to its
    right wraps around below into the port's outward side; everything else takes
    the U-shaped bypass.
    """
    edge, src, tgt, ctx, graph = f.edge, f.src, f.tgt, f.ctx, f.graph
    assert f.src_col is not None and f.tgt_col is not None
    if (
        f.entry_side is PortSide.LEFT
        and f.src_row is not None
        and f.tgt_row is not None
        and f.tgt_row == f.src_row + 1
    ):
        exclude = {sid for sid in (src.section_id, tgt.section_id) if sid is not None}
        if not _h_segment_crosses_other_section(graph, f.sx, f.tx, f.ty, exclude):
            return _route_l_shape(edge, src, tgt, f.i, f.n, ctx)
        if f.left_entry_from_right:
            # Entry-Y blocked: return through the clear inter-row gap as a
            # concentric serpentine wrap.  The below-row U dive cannot fan a
            # bundle leaving a shared exit port (collinear lead-out) and
            # collapses its lines onto one channel.
            return _route_left_entry_family(f)
    if f.right_entry_from_left:
        return _route_right_entry_cross_row(f)
    if f.left_entry_from_right and f.is_left_exit:
        return _route_left_exit_around_below_left_entry(edge, src, tgt, ctx)
    return _route_bypass(edge, src, tgt, f.i, f.src_col, f.tgt_col, ctx, f.src_row)


def _section_right_edge(graph: MetroGraph, station: Station) -> float:
    """The right edge X of *station*'s section, falling back to its own X."""
    section = graph.sections.get(station.section_id) if station.section_id else None
    if section and section.bbox_w > 0:
        return section.bbox_x + section.bbox_w
    return station.x


def _section_left_edge(graph: MetroGraph, station: Station) -> float:
    """The left edge X of *station*'s section, falling back to its own X."""
    section = graph.sections.get(station.section_id) if station.section_id else None
    if section and section.bbox_w > 0:
        return section.bbox_x
    return station.x


def _right_entry_drop_in_is_clear(
    graph: MetroGraph,
    src: Station,
    entry_port: Station,
    corner_x: float,
) -> bool:
    """Whether a RIGHT entry can be reached by a straight drop down *corner_x*.

    Viable only when the source already sits past the target's right edge, so
    the descent channel ``corner_x`` lands on the port's outward side: a single
    drop from the source Y to the entry Y, then a leftward turn into the port,
    crossing no section interior.  Both legs are checked against every other
    section so an intervening box in the descent column (or under the inward
    turn) defers to the gap-above / around-below loops instead.
    """
    ex, ey = entry_port.x, entry_port.y
    if corner_x < _section_right_edge(graph, entry_port) - COORD_TOLERANCE:
        return False
    exclude = {
        sid for sid in (src.section_id, entry_port.section_id) if sid is not None
    }
    if _v_segment_crosses_other_section(graph, corner_x, src.y, ey, exclude):
        return False
    return not _h_segment_crosses_other_section(graph, corner_x, ex, ey, exclude)


def _right_entry_corridor_drop_in_is_clear(
    graph: MetroGraph, src: Station, entry_port: Station, descent_x: float
) -> bool:
    """Whether a source LEFT of a RIGHT entry can drop straight down *descent_x*.

    The wrap variant of :func:`_right_entry_drop_in_is_clear`: here the source
    sits to the LEFT of the port, so reaching the corridor (right of the
    target's edge) means a rightward lead-in across to ``descent_x`` before the
    drop.  On top of the straight-descent and inward-turn clearances, that
    lead-in horizontal at the source Y must clear every other section too.  When
    all three hold the wrap's inter-row staging channel is redundant and the
    descent reads as one straight run from the top corner.
    """
    exclude = {
        sid for sid in (src.section_id, entry_port.section_id) if sid is not None
    }
    return _right_entry_drop_in_is_clear(
        graph, src, entry_port, descent_x
    ) and not _h_segment_crosses_other_section(graph, src.x, descent_x, src.y, exclude)


def _route_right_entry_drop_in(
    edge: Edge,
    src: Station,
    entry_port: Station,
    ctx: _RoutingCtx,
    *,
    pos_n: int,
    delta: float,
    corner_x: float,
) -> RoutedPath:
    """Route a RIGHT entry by dropping straight down the source's outward side.

    Used when the source sits above and past the target's right edge with no
    section in the way (:func:`_right_entry_drop_in_is_clear`).  The R-D-L path
    leads right out of the source, drops down the lead-out channel directly to
    the entry Y, then turns left into the RIGHT port from ``x >= port.x``::

        (sx, sy)        -> H lead-in right of the source
        (corner_x, sy)  ; turn down
        (corner_x, ey)  -> V straight to the entry Y
        (ex, ey)        -> H into the port from its own outward side

    The bundle stagger (*pos_n*, *delta*) and lead-out *corner_x* come from the
    caller's single :func:`_wrap_fan_geometry` resolution, shared with the
    viability check.
    """
    sx, sy = src.x, src.y
    ex, ey = entry_port.x, entry_port.y
    src_off = _get_offset(ctx, edge.source, edge.line_id)
    tgt_off = _get_offset(ctx, edge.target, edge.line_id)
    centerline = [
        (sx, sy + src_off + delta),
        (corner_x, sy + src_off + delta),
        (corner_x, ey + tgt_off - delta),
        (ex, ey + tgt_off - delta),
    ]
    route = route_along(
        edge,
        [(edge, edge.line_id, -delta)],
        centerline,
        base_radius=ctx.curve_radius,
        bundle_offsets=fan_offsets(pos_n, ctx.offset_step),
    )
    assert route is not None  # the lone member is always in its own bundle
    _declare_channel(route, ctx, corner_x, vertical_direction(ey - sy))
    return route


def _left_exit_step_offsets(
    graph: MetroGraph, edge: Edge, src: Station, ctx: _RoutingCtx
) -> tuple[list[str], dict[str, float], dict[str, float], float]:
    """Shared geometry of a LEFT-exit -> RIGHT-entry staircase.

    Returns the bundle's line ids, each line's source and target station offset,
    and the descent channel X.  Each line drops at ``cx + exit_off`` -- the same
    scalar that orders the port fan -- so the westmost leg carries the topmost
    line and the eastmost leg lands ``curve_radius`` clear of the source port.
    """
    _members, line_ids, _edge_by_line = gather_member_edges(graph, edge)
    exit_offs = {lid: _get_offset(ctx, edge.source, lid) for lid in line_ids}
    entry_offs = {lid: _get_offset(ctx, edge.target, lid) for lid in line_ids}
    cx = src.x - ctx.curve_radius - max(exit_offs.values())
    return line_ids, exit_offs, entry_offs, cx


def _left_exit_right_entry_step_is_clear(
    graph: MetroGraph, edge: Edge, src: Station, tgt: Station, ctx: _RoutingCtx
) -> bool:
    """Whether the LEFT-exit staircase descent reaches the RIGHT entry cleanly.

    Every descent leg and the inward turn at the entry Y must clear all other
    sections, and the whole fan must sit on the port's outward side (past the
    target section's right edge), so a blocked descent defers to the wrap /
    around-below fallbacks instead.
    """
    line_ids, exit_offs, _entry_offs, cx = _left_exit_step_offsets(
        graph, edge, src, ctx
    )
    exclude = {sid for sid in (src.section_id, tgt.section_id) if sid is not None}
    drop_xs = [cx + exit_offs[lid] for lid in line_ids]
    if min(drop_xs) < _section_right_edge(graph, tgt) - COORD_TOLERANCE:
        return False
    for dx in (min(drop_xs), max(drop_xs)):
        if _v_segment_crosses_other_section(graph, dx, src.y, tgt.y, exclude):
            return False
    return not _h_segment_crosses_other_section(
        graph, min(drop_xs), tgt.x, tgt.y, exclude
    )


def _route_left_exit_right_entry_step(
    edge: Edge, src: Station, tgt: Station, ctx: _RoutingCtx
) -> RoutedPath | None:
    """Staircase from a LEFT exit port into a lower RIGHT entry (H-V-H).

    The lines arrive at the left-edge exit port in their feed order and must
    keep it into the target's RIGHT port below.  Each line fans by its own
    offset on every leg -- its source offset on the exit run and the descent,
    its target offset on the entry run -- so the bundle steps west, down, then
    west into the port without inverting (the descent fans on the same scalar
    that orders the ports, so no line crosses a bundle-mate)::

        (sx, sy + so)  -> H out of the exit port
        (cx + so, sy + so)  ; turn down
        (cx + so, ey + to)  -> V down the fanned channel
        (ex, ey + to)  -> H into the RIGHT port from its outward side
    """
    sx, sy = src.x, src.y
    ex, ey = tgt.x, tgt.y
    line_ids, exit_offs, entry_offs, cx = _left_exit_step_offsets(
        ctx.graph, edge, src, ctx
    )

    def leg_offsets(line_id: str) -> list[float]:
        return [-exit_offs[line_id], -exit_offs[line_id], -entry_offs[line_id]]

    centerline = [(sx, sy), (cx, sy), (cx, ey), (ex, ey)]
    route = route_offset(
        edge,
        [(edge, edge.line_id, leg_offsets(edge.line_id))],
        centerline,
        base_radius=ctx.curve_radius,
        bundle_offsets=[leg_offsets(lid) for lid in line_ids],
    )
    if route is not None:
        _declare_channel(route, ctx, cx, vertical_direction(ey - sy))
    return route


def _route_left_exit_around_below_left_entry(
    edge: Edge, src: Station, tgt: Station, ctx: _RoutingCtx
) -> RoutedPath | None:
    """Wrap a LEFT exit around below the target into a far-side LEFT entry.

    A reverse-flow bypass (source to the RIGHT of the target, past one or more
    intervening sections) whose target entry sits on the FAR (left) edge: the
    lines leave the source's left-edge exit travelling away from it, drop below
    every intervening section, run left under the target, and rise on the
    target's far side to enter the LEFT port from its own outward side.  A
    U-shaped bypass would instead rise in the gap to the target's right and rake
    its delivery leftward through the box interior.

    Like :func:`_route_left_exit_right_entry_step`, each line fans by its own
    offset per leg -- its source offset out of the exit, down, and along the
    under-run, its target offset up into the port -- so the loop's
    opposite-handed corners cannot invert the bundle::

        (sx, sy)  -> H out of the LEFT exit port (leftward)
        (cx, sy)  ; turn down
        (cx, by)  -> V down past the target row's bottom
        (vx, by)  -> H left under the target
        (vx, ey)  -> V up to the entry Y
        (ex, ey)  -> H right into the LEFT port from its outward side
    """
    graph = ctx.graph
    sx, sy = src.x, src.y
    ex, ey = tgt.x, tgt.y
    _members, line_ids, edge_by_line = gather_member_edges(graph, edge)
    exit_offs = {lid: _get_offset(ctx, edge.source, lid) for lid in line_ids}
    entry_offs = {lid: _get_offset(ctx, edge.target, lid) for lid in line_ids}
    n = len(line_ids)

    src_col, src_row = _resolve_section_colrow(graph, src)
    tgt_col = _resolve_section_col(graph, tgt)
    tgt_row = _resolve_section_row(graph, tgt)
    bw = bundle_width(n, ctx.offset_step)

    # Descent channel in the inter-column gap just LEFT of the source.  The exit
    # taper fans the drop rightward by the exit offset, so the channel's left
    # member is at ``cx`` and the box-near member at ``cx + max(exit_offs)``;
    # shift the gap midline left by half that spread to centre the fan in the
    # gap and keep both flanks clear.
    max_exit = max(exit_offs.values(), default=0.0)
    if src_col is not None and src_col > 0:
        gap_left, gap_right = column_gap_edges(graph, src_col - 1, src_col, row=src_row)
        cx = symmetric_bundle_midpoint(gap_left, gap_right, [bw], 0) - max_exit / 2
        # An empty column left of the source balloons the gap to the canvas edge,
        # dragging the midpoint into a wide same-row section that occupies that
        # space; keep the descent hugging the source's own left edge so it stays
        # clear of that box.
        hug = gap_right - ctx.curve_radius - ctx.offset_step - max_exit
        cx = max(cx, hug)
    else:
        cx = _left_entry_descent_x(ctx, _section_left_edge(graph, src) - max_exit, n)

    # Under-run Y below the sections the loop passes.  A cross-row wrap clears
    # every box in the column range; a same-row wrap only needs to dip below the
    # target box it loops around, so diving below the whole row (and any trunk
    # running along the canvas bottom) is a gratuitous, colliding detour.  The
    # descent hugs the source's own left edge, so the under-run spans the target
    # out to just left of the source -- never the source's column, which sits to
    # the descent's right and so is not passed under.
    cross_row = src_row is not None and tgt_row is not None and src_row != tgt_row
    tgt_c = tgt_col if tgt_col is not None else (src_col if src_col is not None else 0)
    src_c = src_col if src_col is not None else 0
    under_hi_col = src_c if cross_row else max(src_c - 1, tgt_c)
    by = bypass_bottom_y(
        graph,
        under_hi_col,
        tgt_c,
        BYPASS_CLEARANCE,
        src_row=src_row,
        cross_row=cross_row,
        tgt_row=tgt_row,
    )

    # Ascent channel left of the target box.  The entry taper fans the lines on
    # the ascent by their entry offset, so the line nearest the box sits
    # ``max(entry_offs)`` to the channel's right; anchor on the box edge less
    # that spread so even that line keeps a full curve radius of run into the
    # port.
    max_entry = max(entry_offs.values(), default=0.0)
    vx = _left_entry_descent_x(ctx, _section_left_edge(graph, tgt) - max_entry, n)

    # West out of the exit, around below, then east into the LEFT port is a net
    # half-turn that transposes the bundle end-to-end.  The destination section
    # takes the transposed order from the seam classifier (``_reorder_reconvergence``),
    # so the entry offsets here are already the transposed order; the bundle tapers from
    # its exit offset out of the source to that entry offset into the port, the
    # taper following the loop's natural transpose so no line crosses a mate.
    members = [
        (edge_by_line[lid], lid, -exit_offs[lid], entry_offs[lid]) for lid in line_ids
    ]
    centerline = [(sx, sy), (cx, sy), (cx, by), (vx, by), (vx, ey), (ex, ey)]
    route = route_tapered(
        edge, members, centerline, transition_leg=3, base_radius=ctx.curve_radius
    )
    if route is not None:
        _declare_channel(route, ctx, cx, Direction.D)
        _declare_channel(route, ctx, vx, Direction.U)
    return route


def _route_right_entry_cross_row(f: _InterFacts) -> RoutedPath | None:
    """Cross-row feed into a RIGHT entry, reached from the port's outward side.

    A standard L-shape drops its vertical channel across the source or target
    box to reach the far-edge RIGHT port.  When the source already sits past
    the target's right edge with a clear descent column, drop straight down its
    outward side to the entry Y and turn in.  Otherwise run the long horizontal
    in the clear inter-row band just above the target row (then drop down the
    target's right side into the port) when that band is clear, else loop
    around below the whole target row.  Every approach enters the RIGHT port
    from ``x >= port.x`` and never crosses a section interior.

    The dispatch rule guarantees ``src_row < tgt_row`` and, by ceding obstacle
    cases to the earlier bypass / plough rules, that no section sits between the
    source and the port; the outward-side drop-in is therefore the usual path,
    and the gap-above / around-below fallbacks cover only an exotic descent
    blocked by a wide same-column sibling.

    A LEFT-side exit port is the exception: its lines reach the port travelling
    away from the box (the stations sit to the port's right) and leave it
    travelling the same way into the target's RIGHT port below, so the route
    steps west -> down -> west -- two opposite-handed corners.  A concentric
    bundle inverts its nesting through opposite turns, crossing the lines at the
    port; the staircase builder fans each leg by its own offset so every line
    keeps the feed order on both ports and stays parallel through the descent.
    """
    edge, src, tgt, ctx, graph = f.edge, f.src, f.tgt, f.ctx, f.graph
    if f.is_left_exit and _left_exit_right_entry_step_is_clear(
        graph, edge, src, tgt, ctx
    ):
        return _route_left_exit_right_entry_step(edge, src, tgt, ctx)
    _fan, pos_n, delta, corner_x = _wrap_fan_geometry(
        ctx, edge, src, f.i, f.n, vertical_direction(tgt.y - src.y)
    )
    if _right_entry_drop_in_is_clear(graph, src, tgt, corner_x):
        return _route_right_entry_drop_in(
            edge, src, tgt, ctx, pos_n=pos_n, delta=delta, corner_x=corner_x
        )
    if f.tgt_row is not None and _right_entry_gap_above_is_clear(
        graph, src, tgt, tgt, f.tgt_row
    ):
        return _route_right_entry_via_gap_above(
            edge, src, tgt, tgt, f.i, f.n, ctx, f.tgt_row
        )
    return _route_right_entry_around_below(edge, src, tgt, tgt, f.i, f.n, ctx)


def _route_left_entry_family(f: _InterFacts) -> RoutedPath | None:
    """Cross-row feed into a LEFT entry from a source on its right.

    A standard L-shape would cut through the target interior to reach the
    left-side port.  Wrap leftward through the inter-row gap; when that gap
    horizontal lands inside an intervening section, descend the clear corridor
    if one exists, else loop around below the target.
    """
    edge, src, tgt, ctx, graph = f.edge, f.src, f.tgt, f.ctx, f.graph
    # A LEFT-side exit already faces outward toward the LEFT entry: lead it out
    # leftward and drop straight down a channel clear of both boxes, never the
    # rightward-lead-out wrap, whose leftward channel run would claw back across
    # a tall source box (a folded TB bridge feeding a sink below and to the left).
    if f.is_left_exit:
        return _route_left_exit_left_entry_drop(edge, src, tgt, ctx)
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
# docs/dev/inter_section_dispatch.mdx.
_INTER_SECTION_RULES: list[_Rule] = [
    # A perpendicular (TOP/BOTTOM) exit leaves vertically: route it before the
    # same-Y shortcut, which would graze both boxes when exit and entry share an
    # edge Y.
    _Rule(
        "perp-exit",
        lambda f: f.is_perp_exit,
        lambda f: _route_perp_exit(f.edge, f.src, f.tgt, f.src_col, f.tgt_col, f.ctx),
    ),
    # A TB/BT trailing perp exit feeding an entry against the flow (a side entry
    # at/above a downward exit, or a perpendicular entry on the target's far
    # side) cannot be reached by the flow-direction drop without grazing the
    # exit edge or clawing back through the box, so it takes the up/down-and-over
    # corridor shape before the same-Y shortcut and the TB bottom-exit drop below
    # claim it.
    _Rule(
        "TB perp-exit over",
        lambda f: f.is_tb_perp_exit_against_flow,
        lambda f: _route_perp_exit_over(f.edge, f.src, f.tgt, f.ctx),
    ),
    # Same Y, no obstacle, not a right-entry plough: a straight horizontal run.
    _Rule(
        "same-Y straight",
        lambda f: f.same_y and not f.needs_bypass and not f.right_entry_from_left,
        _route_straight_connector,
    ),
    # A trailing perp (TOP/BOTTOM) exit feeding a LEFT/RIGHT entry on the
    # target's far side wraps through the inter-row gap and around the target
    # box, approaching the port horizontally from its outward side.  Offset-
    # independent, so it claims this class in both the validate and render
    # routing paths (unlike the offset-gated TB bottom-exit drop below); placed
    # before that drop and the LEFT/RIGHT-entry wrap families, whose flow-
    # direction drop / sideways lead-out mis-route this shape.
    _Rule(
        "perp-exit -> far-side entry wrap",
        lambda f: f.is_perp_exit_farside_entry_wrap,
        lambda f: _route_perp_exit_farside_entry_wrap(f),
    ),
    # A TB bottom-exit drop whose column has sections stacked between the source
    # and the (folded-below) target diverts around them through a clear gap; the
    # plain straight drop below would plough those boxes.  Checked first so only
    # the obstructed feeders divert and adjacent ones keep the straight drop.
    _Rule(
        "TB bottom exit around stack",
        lambda f: f.tb_bottom_exit_drops_through_stack,
        lambda f: _route_tb_bottom_exit_around_stack(f),
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
    # Same X, but NOT a stacked LEFT-exit -> LEFT-entry: that pair shares the
    # column's left-edge X, so a straight vertical drop would run down the
    # source section's own edge and through its box; the serpentine rule below
    # leads it out into a clear left-of-column channel instead.
    _Rule(
        "same-X vertical drop",
        lambda f: f.same_x and not f.is_serpentine_left_exit_left_entry,
        _route_straight_connector,
    ),
    _Rule(
        "bottom-exit junction",
        lambda f: f.edge.source in f.ctx.bottom_exit_junctions,
        lambda f: _route_bottom_exit_junction(f.edge, f.src, f.tgt, f.i, f.n, f.ctx),
    ),
    # Every feeder of a merge that has a trunk routes through the merge
    # handlers so the converging line is a single stroke: the trunk carries the
    # full bypass to the entry port, every other feeder descends onto the
    # trunk's channel.  These precede the bypass / merge-entry rules, which
    # would otherwise route a non-bypass feeder straight into the entry on its
    # own lateral slot (a second parallel stroke).
    _Rule("merge trunk", lambda f: f.is_merge_trunk, _route_merge_trunk_feeder),
    _Rule("merge branch", lambda f: f.is_merge_branch, _route_merge_branch_feeder),
    _Rule("bypass family", lambda f: f.needs_bypass, _route_bypass_family),
    _Rule(
        "near-vertical same-col junction",
        lambda f: f.takes_near_vertical_junction_drop,
        _route_near_vertical_junction,
    ),
    # RIGHT entry fed from the left: wrap around the right side (over the top for
    # a same-row source, below the source row for a cross-row one) rather than
    # cut through the interior.
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
    # A LEFT exit reaching a far-side LEFT entry to its left with no intervening
    # section to hop (adjacent or same-row columns, so ``needs_bypass`` is False
    # and the bypass family never claims it): a straight L-shape ploughs the
    # target box to reach its far-edge port.  Wrap around below into the port's
    # own outward side, the same shape the bypass family uses for the multi-hop
    # case.
    _Rule(
        "LEFT exit -> far-side LEFT entry wrap",
        lambda f: f.left_entry_from_right and f.is_left_exit,
        lambda f: _route_left_exit_around_below_left_entry(f.edge, f.src, f.tgt, f.ctx),
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
    # A feed from a row ABOVE into a RIGHT entry one or more rows down, from a
    # source on the port's RIGHT (travelling left) with no intervening section to
    # bypass: the standard L-shape drops its vertical channel across the source
    # box to reach the far-edge port.  Run the long horizontal in the band above
    # the target (or around below it) so the port is entered from its outward
    # side.  This rule carries no obstacle test, so the plough rule (earlier)
    # claims the with-obstacle cases and this is the obstacle-free remainder.
    _Rule(
        "RIGHT entry cross-row wrap",
        lambda f: (
            f.entry_side is PortSide.RIGHT
            and f.horizontal is Direction.L
            and f.src_row is not None
            and f.tgt_row is not None
            and f.src_row < f.tgt_row
        ),
        _route_right_entry_cross_row,
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
        route = rule.route(f)
    elif (
        _perp_multi_side_entry_side(ctx.graph, edge.target) is not None
        and f.src_col is not None
        and f.tgt_col is not None
    ):
        # No dispatch rule claimed a perpendicular entry on a multi-side
        # section: the standard L-shape fallback would drop to the boundary and
        # slide into the port along it.  Route via the U-bypass instead, which
        # drops straight into the port from beyond the boundary (see the perp
        # branch in ``_route_bypass``).  A rule-claimed edge keeps its handler.
        route = _route_bypass(edge, src, tgt, f.i, f.src_col, f.tgt_col, ctx, f.src_row)
    else:
        # Standard L-shape: the default when no rule above claims the edge.
        route = _route_l_shape(edge, src, tgt, f.i, f.n, ctx)
    _declare_trunk(route, ctx)
    return route


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
    if needs_perp_approach_fan(ctx.graph, edge.target):
        return _route_tb_bottom_exit_approach_fan(edge, src, tgt, ctx)

    # The drop continues the source section's own rotation lane (x - off) out of
    # the BOTTOM port, so the trunk and its outgoing bundle share one lane.  A
    # horizontal-flow target's perp-entry drop aligns to this feeder lane (via
    # the crossing-X in _perp_drop_x), so the bundle stays on the same per-line X
    # straight through the seam.
    x_off = _tb_x_offset(ctx, edge.source, edge.line_id, src.section_id)
    sx = src.x + x_off
    sy = src.y
    tx = tgt.x + x_off
    ty = tgt.y

    if abs(tx - sx) <= COORD_TOLERANCE:
        return route_along(
            edge,
            [(edge, edge.line_id, 0.0)],
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

    # Fan the whole co-travelling bundle off one centreline at the section's
    # trunk X (offset 0) so the builder rotates each line's lane offset around
    # both corners: the vertical legs separate in X and the horizontal jog
    # separates in Y.  The first leg's right-hand normal points in -X for a
    # downward drop, so the lane offset is signed to land each riser back on its
    # own trunk X.
    _members, line_ids, _edge_by_line = gather_member_edges(ctx.graph, edge)
    drop_sign = 1.0 if dy >= 0 else -1.0
    riser_sign = -drop_sign

    def lane_offset(line_id: str) -> float:
        return riser_sign * _tb_x_offset(ctx, edge.source, line_id, src.section_id)

    # The fan widens the jog channel toward the source section (the BOTTOM port
    # sits on that near edge at sy), so push the channel a bundle width past the
    # edge in the drop direction so even the innermost line clears it.  Then
    # clamp the channel strictly between the two ports, keeping both vertical
    # legs positive-length for the corner curves to bite into.
    fan_clearance = INTER_ROW_EDGE_CLEARANCE + (len(line_ids) - 1) * ctx.offset_step
    hy = sy + drop_sign * max((hy - sy) * drop_sign, fan_clearance)
    lo, hi = (sy, ty) if dy >= 0 else (ty, sy)
    hy = min(max(hy, lo + ctx.curve_radius), hi - ctx.curve_radius)

    return route_along(
        edge,
        [(edge, edge.line_id, lane_offset(edge.line_id))],
        [(src.x, sy), (src.x, hy), (tgt.x, hy), (tgt.x, ty)],
        base_radius=ctx.curve_radius,
        bundle_offsets=[lane_offset(lid) for lid in line_ids],
    )


def _route_tb_bottom_exit_approach_fan(
    edge: Edge, src: Station, tgt: Station, ctx: _RoutingCtx
) -> RoutedPath | None:
    """Drop from a TB BOTTOM exit onto a distinct-line port's approach channel.

    At a distinct-line perp entry (:func:`needs_perp_approach_fan`) the feeders
    each carry one line and all leave the same column trunk, so their feeder
    lanes coincide on one X.  Land each on its own approach channel instead --
    the per-line X :func:`perp._perp_approach_fan_x` pins the intra-section
    drop to -- so the distinct lines ride parallel channels into the port rather
    than overlaying one vertical channel.

    A feeder leaves the BOTTOM port downward, jogs across the inter-row gap onto
    its channel, then drops in, so any lateral step turns through bounded corners
    rather than a raw diagonal.  A feeder already on its channel has a zero-width
    jog, which the bundle builder collapses to a clean straight drop.
    """
    land_x = _perp_approach_fan_x(ctx, edge.target, edge.line_id, tgt.x)
    sy, ty = src.y, tgt.y

    dy = ty - sy
    hy = inter_row_channel_y(ctx.graph, src, tgt, sy, ty, dy, ctx.curve_radius)
    lo, hi = (sy, ty) if dy >= 0 else (ty, sy)
    hy = min(max(hy, lo + ctx.curve_radius), hi - ctx.curve_radius)
    return route_along(
        edge,
        [(edge, edge.line_id, 0.0)],
        [(src.x, sy), (src.x, hy), (land_x, hy), (land_x, ty)],
        base_radius=ctx.curve_radius,
    )


def _around_stack_channel_x(f: _InterFacts) -> float:
    """X of a descent channel just left of the feeder's stacked column.

    Seated a corner-and-step left of the column's leftmost edge -- so the
    descent runs in the gap to the column's left, clearing every box stacked in
    it (the section headers sit on the right, so the left gap is the open side).
    Mirrors :func:`_route_left_exit_left_entry_drop`, which places its channel
    the same way for a folded TB bridge feeding a convergence sink.
    """
    left_edge = col_left_edge(f.graph, f.src_col, default=f.sx)
    return left_edge - f.ctx.curve_radius - f.ctx.offset_step


def _route_tb_bottom_exit_around_stack(f: _InterFacts) -> RoutedPath | None:
    """Route a TB bottom-exit feeder around sections stacked below it.

    The flow-direction drop would plough the branch boxes stacked between this
    feeder and a convergence sink folded onto a lower row of the same column.
    Divert through the clear inter-column gap beside the column instead::

        (sx, sy)             leave the BOTTOM port
        (sx, cy_down)        drop into the gap below the source row
        (vx, cy_down)        jog out to the clear gap channel
        (vx, cy_entry)       descend past every intervening box
        (tx, cy_entry)       jog back over the target in the gap above it
        (tx, ty)             drop into the TOP entry port

    Each co-travelling line rides the source section's rotation lane, fanned off
    one centreline so the final drop lands on the same per-line X as the
    adjacent straight-drop feeders converging on the shared port.  Where distinct
    lines share the entry (:func:`needs_perp_approach_fan`) that shared X is the
    per-line approach channel (:func:`perp._perp_approach_fan_x`) instead of the
    feeder lane, since every feeder sits on one column trunk.
    """
    edge, src, tgt, ctx, graph = f.edge, f.src, f.tgt, f.ctx, f.graph
    sx, sy, tx, ty = f.sx, f.sy, f.tx, f.ty
    src_sec = resolve_section(graph, src)
    tgt_sec = resolve_section(graph, tgt)
    # Guaranteed by the predicate, which fires only for a vertical-flow exit.
    assert src_sec is not None and tgt_sec is not None and f.src_col is not None

    _members, line_ids, _edge_by_line = gather_member_edges(graph, edge)

    fans_distinct = needs_perp_approach_fan(graph, edge.target)
    if fans_distinct:
        tx = _perp_approach_fan_x(ctx, edge.target, edge.line_id, tgt.x)

    def lane_offset(line_id: str) -> float:
        # Negated so the down-leg's right-hand normal lands each riser on its
        # own trunk X.  Where distinct lines fan, the per-line channel is baked
        # into ``tx`` (each feeder carries one line), so the lane fan is zero.
        if fans_distinct:
            return 0.0
        return -_tb_x_offset(ctx, edge.source, line_id, src.section_id)

    # The bundle fan lifts the jog's innermost line toward the source box, so
    # seat its corridor a fan width below the bottom edge to hold the clearance.
    src_bottom = src_sec.bbox_y + src_sec.bbox_h
    fan_clearance = INTER_ROW_EDGE_CLEARANCE + (len(line_ids) - 1) * ctx.offset_step
    cy_down = max(
        header_corridor_y(
            graph,
            src_sec.grid_row,
            below=True,
            base_radius=ctx.curve_radius,
            default=sy,
            col=f.src_col,
        ),
        src_bottom + fan_clearance + ctx.curve_radius,
    )
    cy_entry = header_corridor_y(
        graph, tgt_sec.grid_row, below=False, base_radius=ctx.curve_radius, default=ty
    )
    vx = _around_stack_channel_x(f)

    route = route_along(
        edge,
        [(edge, edge.line_id, lane_offset(edge.line_id))],
        [
            (sx, sy),
            (sx, cy_down),
            (vx, cy_down),
            (vx, cy_entry),
            (tx, cy_entry),
            (tx, ty),
        ],
        base_radius=ctx.curve_radius,
        bundle_offsets=[lane_offset(lid) for lid in line_ids],
    )
    _declare_channel(route, ctx, vx, Direction.D)
    return route


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
    vx = src.x + sum(exit_offs) / len(exit_offs)
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
    """Truncated descent from a feeder junction onto the trunk's channel.

    Every non-trunk feeder of a merge drops to the trunk's bypass channel
    (``trunk_by``) and turns along it toward the entry port, so the converging
    line overlays the trunk as a single stroke.  The lead-in leaves on the gap
    side the feeder junction already sits on (junctions are placed in the
    inter-column gap downstream of their fork); leading toward the entry
    instead would re-enter the source section.  The tail turns toward the entry
    port so it overlaps the trunk's horizontal run; same-column feeders are
    then snapped onto the trunk's exact descent channel by
    :func:`_coincide_same_line_tracks`.
    """
    graph = ctx.graph
    sx, sy = src.x, src.y
    src_off = _get_offset(ctx, edge.source, edge.line_id)
    by = ctx.merge.trunk_by.get(edge.target, sy)

    # Descend just outside the source section on the side the junction sits on.
    left_edge = col_left_edge(graph, src_col)
    right_edge = col_right_edge(graph, src_col)
    if sx >= (left_edge + right_edge) / 2:
        lead_x = max(right_edge + MERGE_ROUTE_MARGIN, sx + ctx.curve_radius)
    else:
        lead_x = min(left_edge - MERGE_ROUTE_MARGIN, sx - ctx.curve_radius)

    # Turn along the channel toward the entry port (the way the trunk runs).
    ep_id = ctx.merge.entry_port_for.get(edge.target)
    ep = graph.stations.get(ep_id) if ep_id else None
    entry_x = ep.x if ep else graph.stations[edge.target].x
    tail_sign = 1.0 if entry_x >= lead_x else -1.0
    tail_x = lead_x + tail_sign * ctx.curve_radius * 2

    # One branch line per call: a single descent with no bundle to fan, so the
    # centreline carries this line's own offset and both corners take the base
    # radius (the concentric radius at zero displacement).
    route = route_along(
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
    _declare_channel(route, ctx, lead_x, vertical_direction(by - sy))
    return route


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
    n: int,
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

    A LEFT entry port with no clear inter-column channel to its left (the
    target sits in the leftmost column, fed from its right) has no gap for the
    bypass to rise in on the port's own side; the U-shape's gap2 lands to the
    RIGHT of the box and its final port-approach leg ploughs leftward through
    the target interior.  Route such a trunk around below the target instead,
    rising on the far (left) side and entering the LEFT port from outside.  The
    around-below traverse runs at the trunk's ``bypass_bottom_y`` channel, the
    same Y the branch feeders drop onto, so the converging lines overlay as one
    stroke.

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

    if ep is not None and ep_port is not None and ep_port.side == PortSide.LEFT:
        ep_col, ep_row = _resolve_section_colrow(ctx.graph, ep)
        no_left_channel = (
            ep_col is None
            or ep_row is None
            or _corridor_descent_x(ctx, ep_col, ep_row, 0.0) is None
        )
        if no_left_channel:
            trunk_by = ctx.merge.trunk_by.get(edge.target)
            around = _route_around_section_below(
                edge, src, tgt, ep, i, n, ctx, channel_y=trunk_by
            )
            assert around is not None  # the trunk is always its own bundle member
            return around
    force_cross_row = merge_trunk_force_cross_row(
        ctx.graph, src_col, tgt_col, src_row, tgt_row
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


def _bottom_row_climb_corridor_clear(
    graph: MetroGraph,
    src_row: int,
    tgt_row: int,
    src_col: int,
    tgt_col: int,
) -> bool:
    """Whether a bottommost-row source can climb to a higher-row target by
    running along its own row level instead of diving below it.

    True when the source sits in the bottommost content row, the target is in a
    higher row, and no same-row section occupies the columns the rightward run
    would cross.  In that case the intervening sections that classified the edge
    as a bypass are all in higher rows (above a run at the source's Y), so the
    canyon below the source row is clear and the dive is gratuitous.
    """
    if tgt_row >= src_row or src_row != max_grid_row_with_content(graph):
        return False
    return not _has_intervening_sections(graph, src_col, tgt_col, src_row)


def _perp_multi_side_entry_side(graph: MetroGraph, target_id: str) -> PortSide | None:
    """The side of a TOP/BOTTOM entry port on a multi-side-entry section.

    Returns ``PortSide.TOP``/``PortSide.BOTTOM`` when *target_id* is a
    perpendicular entry port whose section is entered from more than one side
    (some lines enter through a flow-axis side, others through this perp one),
    else ``None``.  A perpendicular entry is reached by a straight drop into
    the port from beyond the section boundary; a section entered from a single
    side -- where the perp port carries every entering line -- is not treated
    specially and keeps its existing routing.
    """
    port = graph.ports.get(target_id)
    if port is None or not port.is_entry:
        return None
    if port.side not in (PortSide.TOP, PortSide.BOTTOM):
        return None
    section = graph.sections.get(port.section_id)
    if section is None:
        return None
    sides = {graph.ports[pid].side for pid in section.entry_ports if pid in graph.ports}
    return port.side if len(sides) > 1 else None


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
    src_sec = resolve_section(graph, src, prefer_upstream=False)
    tgt_sec = resolve_section(graph, tgt, prefer_upstream=False)
    src_sec_id = src_sec.id if src_sec is not None else None
    tgt_sec_id = tgt_sec.id if tgt_sec is not None else None

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

    # A bottommost-row source climbing to a higher-row target keeps its run at
    # its own Y when the row level to the right is clear: the sections that
    # forced the bypass classification sit in higher rows, above this run, so
    # diving below the source row and climbing back up is a gratuitous dogleg.
    # A merge/fan junction target collects feeders onto a shared trunk below the
    # row, so this only applies to a route landing on a real section entry port.
    src_off = _get_offset(ctx, edge.source, edge.line_id)
    tgt_entry = graph.ports.get(edge.target)
    if (
        cross_row
        and src_row is not None
        and tgt_row is not None
        and tgt_entry is not None
        and tgt_entry.is_entry
        and _bottom_row_climb_corridor_clear(graph, src_row, tgt_row, src_col, tgt_col)
    ):
        # Keep the run on the line's in-section track (its per-line offset), not
        # the bare port-marker row, so it leaves the exit corner straight rather
        # than stepping off by ``src_off``. The source offsets already separate
        # co-travelling lines, so the below-row traverse's nest separation would
        # double up here.
        base_y = sy + src_off
        nest_offset = 0.0
    elif (
        src_row is not None
        and tgt_row is not None
        and src_row == tgt_row
        and tgt_entry is not None
        and tgt_entry.is_entry
        and base_y < sy - COORD_TOLERANCE
        and not _h_segment_crosses_other_section(
            graph,
            sx,
            effective_tx,
            sy,
            {sid for sid in (src.section_id, tgt.section_id) if sid is not None},
            margin=BYPASS_CLEARANCE,
        )
    ):
        # Same-row bypass whose source sits below the intervening sections that
        # forced the classification: the computed lane hugs their bottoms above
        # the source, so diving up to it at the exit and stepping up again into
        # the target is an avoidable kink.  The source row threads clear across
        # the span, so run straight along it and turn up once at the target.
        base_y = sy + src_off
        nest_offset = 0.0

    # A perpendicular (TOP/BOTTOM) entry on a section entered from more than one
    # side is reached by a straight drop into the port from beyond the section
    # boundary.  The bypass otherwise seats its traverse at a level that clears
    # the intervening sections but not the taller target box, then slides along
    # the boundary into the port -- a lateral reversal at the crossing.  Carry
    # the traverse past the target's own edge so the final leg is a clean
    # vertical crossing at the port X (pinned via gap2 below).
    perp_entry_side = _perp_multi_side_entry_side(graph, edge.target)
    if perp_entry_side is not None:
        lo_col, hi_col = min(src_col, tgt_col), max(src_col, tgt_col)
        if perp_entry_side is PortSide.BOTTOM:
            # A bottom-port entry only needs room to turn in below the target
            # box (a corner radius), not the full bypass clearance.  Hugging the
            # box keeps the drop-in clear of the deeper below-box fan-in channels
            # that feed later sections -- seated a full clearance below the box,
            # they would otherwise draw flush with a drop-in at that same level.
            traverse = ty + ctx.curve_radius
            for s in graph.sections.values():
                if (
                    s.bbox_w <= 0
                    or not (lo_col <= s.grid_col <= hi_col)
                    or s.id == tgt.section_id
                ):
                    continue
                if (
                    s.bbox_y <= ty + COORD_TOLERANCE
                    and s.bbox_y + s.bbox_h > ty + COORD_TOLERANCE
                ):
                    # A same-row section extends below the target box bottom: run
                    # below it with clearance rather than through it.
                    traverse = max(traverse, s.bbox_y + s.bbox_h + BYPASS_CLEARANCE)
                elif s.bbox_y > ty + COORD_TOLERANCE:
                    # A lower-row section header protrudes up into the gap: never
                    # rise below its safe boundary.
                    traverse = min(
                        traverse,
                        max(section_header_safe_cap(s), ty + ctx.curve_radius),
                    )
            base_y = traverse
        else:
            base_y = min(base_y, ty - BYPASS_CLEARANCE)
        nest_offset = 0.0
        # Land the drop on the exact per-line X the in-section departure
        # leaves the port at, so approach and departure cross the boundary as
        # one straight line (``check_perp_entry_boundary_consistent``).
        crossing_x = _perp_entry_crossing_x(ctx, edge.target, edge.line_id, tx)
        if crossing_x is not None:
            effective_tx = crossing_x

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
    # _materialize_gap_slots pass then re-stacks all inter-section channels
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
                graph,
                src_col,
                src_row,
                g1_n,
                ctx.offset_step,
                anchor_section_id=src_sec_id,
                anchor_side=PortSide.RIGHT,
            )
            gap1_limit = sx + ctx.curve_radius
            if gap1_base - (g1_n - 1) * ctx.offset_step < gap1_limit:
                gap1_mid = gap1_limit + half_g1
            else:
                gap1_mid = gap1_base - half_g1
            off1 = delta1
            gap1_x = gap1_mid + delta1

        gap2_base = _gap_channel_base(
            graph,
            tgt_col - 1,
            tgt_row,
            g2_n,
            ctx.offset_step,
            anchor_section_id=tgt_sec_id,
            anchor_side=PortSide.LEFT,
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
                graph,
                src_col - 1,
                src_row,
                g1_n,
                ctx.offset_step,
                anchor_section_id=src_sec_id,
                anchor_side=PortSide.LEFT,
            )
            gap1_limit = sx - ctx.curve_radius
            if gap1_base + (g1_n - 1) * ctx.offset_step > gap1_limit:
                gap1_mid = gap1_limit - half_g1
            else:
                gap1_mid = gap1_base + half_g1
            off1 = delta1
            gap1_x = gap1_mid + delta1

        gap2_base = _gap_channel_base(
            graph,
            tgt_col,
            tgt_row,
            g2_n,
            ctx.offset_step,
            anchor_section_id=tgt_sec_id,
            anchor_side=PortSide.RIGHT,
        )
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
    exclude = {sid for sid in (src.section_id, tgt.section_id) if sid is not None}
    if cross_row:
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
    else:
        # Same-row bypass past an intervening section whose box is wider than
        # its grid cell: the neighbour cell sits empty, so the gap query bounds
        # the descent channel at the canvas origin and it can land inside that
        # box.  Push gap1 toward the source end of the route and gap2 toward the
        # target end, so the long below-row traverse, not the descent, passes
        # the box.  The current leg X seeds the bound that pins each push.
        if horizontal is Direction.L:
            g1_left, g1_right, g2_left, g2_right = gap1_x, None, None, gap2_x
        else:
            g1_left, g1_right, g2_left, g2_right = None, gap1_x, gap2_x, None
        gap1_x = _clear_channel_x_in_band(
            graph, gap1_x, sy, by, SECTION_ROUTE_CLEARANCE, exclude, g1_left, g1_right
        )
        gap2_x = _clear_channel_x_in_band(
            graph, gap2_x, by, ty, SECTION_ROUTE_CLEARANCE, exclude, g2_left, g2_right
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
    tgt_off = _get_offset(ctx, edge.target, edge.line_id)
    if perp_entry_side is not None:
        # Land the gap2 rise on the departure's crossing column so the final
        # leg is a straight perpendicular crossing sharing the in-section
        # drop's X, not a slide along the boundary.  The rise renders at
        # ``gap2_x - delta2 + delta2 * n3x``, so solve that back to the
        # crossing X (``effective_tx``) whichever way the rise runs.
        n3x_perp = 1.0 if gap2_vertical is Direction.U else -1.0
        gap2_x = effective_tx + delta2 * (1.0 - n3x_perp)
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
    route = route_tapered_anchored(
        (edge, edge.line_id, sigma1, sigma2),
        centerline,
        transition_leg=3,
        base_radius=ctx.curve_radius,
        src_bundle_offsets=src_anchor,
        tgt_bundle_offsets=tgt_anchor,
        normalize_exempt=False,
    )
    _declare_channel(route, ctx, gap1_x, gap1_vertical, g1_j, g1_n)
    _declare_channel(route, ctx, gap2_x, gap2_vertical, g2_j, g2_n)
    return route


def _declare_trunk(route: RoutedPath | None, ctx: _RoutingCtx) -> None:
    """Declare the inter-row gap an inter-section route's horizontal trunk runs in.

    Called once per inter-section route from :func:`_route_inter_section`; a
    no-op for routes with no interior horizontal trunk.  Read from the built
    geometry like :func:`_declare_channel`: the trunk leg's actual Y names its
    gap via :func:`inter_row_gap_upper_row`.  A deep dive that clears every row
    falls in no gap and declares ``gap_upper_row=None``;
    :func:`_materialize_trunk_slots` then groups those by proximity rather than
    a shared gap.
    """
    if route is None:
        return
    trunk = next(iter(iter_horizontal_trunks(route)), None)
    if trunk is None:
        return
    _k, seg = trunk
    route.declare_trunk_slot(gap_upper_row=inter_row_gap_upper_row(ctx.graph, seg.y))


def _declare_channel(
    route: RoutedPath | None,
    ctx: _RoutingCtx,
    x: float,
    direction: Direction,
    slot_index: int = 0,
    n_slots: int = 1,
) -> None:
    """Declare the gap channel a handler just placed at *x* on *route*.

    The handler knows the channel's final X, so the gap it occupies is named by
    :func:`gap_lo_for_x` from the leg's ACTUAL geometry on the built route (the
    segment travelling *direction* nearest *x* -- a per-line offset or clearance
    nudge can carry it into the adjacent gap).  ``slot_index`` / ``n_slots`` are
    provisional -- :func:`_materialize_gap_slots` re-ranks each gap bundle from
    geometry.  A channel that lands outside every inter-column gap (hugging a
    section edge) declares nothing, matching the post-pass which would not have
    bundled it either.
    """
    if route is None:
        return
    down = direction is Direction.D
    best = None
    best_d = None
    for _k, sx, y_lo, y_hi, seg_down in iter_vertical_segments(route):
        if seg_down is not down:
            continue
        d = abs(sx - x)
        if best_d is None or d < best_d:
            best_d, best = d, (sx, y_lo, y_hi)
    if best is None:
        return
    match = gap_lo_for_x(ctx.graph, best[0], best[1], best[2])
    if match is None:
        return
    lo, matched_row = match
    route.declare_gap_slot(
        lo_col=lo,
        hi_col=lo + 1,
        row=matched_row,
        direction=direction,
        slot_index=slot_index,
        n_slots=n_slots,
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
        target_x=tx,
    )

    route = route_hvh_tapered(
        ctx,
        edge,
        src,
        tgt,
        mid_x,
        base_radius=ctx.curve_radius,
        min_radius=COORD_TOLERANCE,
        fit_segment=True,
    )
    _declare_channel(route, ctx, mid_x, vertical_direction(ty - sy))
    return route


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
        target_x=tx,
    )

    # Lead-in long enough for the outermost fan line's first-corner arc; it
    # overlaps the upstream same-line tail (re-joined by the fan-out tail pass),
    # so the extra length is free.  When the graze correction pushed the descent
    # past the junction, extend the lead-in back to the junction (``sx``) so the
    # feeder rejoins there as one horizontal run instead of being dragged out to
    # a floating stub.
    lead_len = ctx.curve_radius + 2 * half_width
    lead_x = mid_x - horizontal.sign * lead_len
    lead_x = min(lead_x, sx) if horizontal.sign > 0 else max(lead_x, sx)
    src_off = _get_offset(ctx, edge.source, edge.line_id)
    tgt_off = _get_offset(ctx, edge.target, edge.line_id)
    centerline = [
        (lead_x, sy + src_off + delta),
        (mid_x, sy + src_off + delta),
        (mid_x, ty + tgt_off + delta),
        (tx, ty + tgt_off + delta),
    ]
    # Not normalize-exempt: L-shape fans from one junction to different targets
    # share the inter-column gap, and _materialize_gap_slots restacks them into
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
    _declare_channel(route, ctx, mid_x, vertical_direction(ty - sy), ui, un)
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

    When a perpendicular entry sits on the far side of the target from the
    exit-side corridor (a BOTTOM exit feeding a TOP entry, or the mirror), a
    straight descent on the trunk X would run through the target's stations.
    Such a route crosses to the inter-column gap, rises/descends there to the
    entry-side corridor outside the box, and turns the final leg into the port
    from the port's own side.

    This is the exit end of the up-and-over shape whose entry end is
    ``tb_handlers._route_perp_entry_from_corridor``; both seat their bundle on
    the per-line lateral from ``perp._perp_riser_lateral`` (see that module for
    the TOP vs BOTTOM sign convention) so the two legs stay parallel across the
    shared port.
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

    def inter_col_gap_x() -> float:
        """X of the gap between the source and target columns."""
        src_col = src_sec.grid_col if src_sec is not None else 0
        tgt_col = tgt_sec.grid_col if tgt_sec is not None else src_col
        return column_gap_midpoint(graph, src_col, tgt_col, row)

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
        assert tgt_port is not None
        entry_above = tgt_port.side == PortSide.TOP
        crosses_box = (cy_base > ty) if entry_above else (cy_base < ty)
        if crosses_box:
            # The exit-side corridor sits on the far side of the target from its
            # entry port, so a straight descent on the trunk X would run up
            # through the target's stations.  Cross to the inter-column gap,
            # switch to the entry-side corridor outside the target box, then turn
            # the final perpendicular leg in from the port's own side.
            gap_x = inter_col_gap_x()
            # The exit-side down-leg drops at the exit X and runs across only to
            # the inter-column gap, so it need clear just the source column's
            # sections, not the row's deepest section in a far column (which
            # would loop the leg to the canvas bottom around a box it never
            # passes under).
            cy_down = (
                header_corridor_y(
                    graph,
                    row,
                    below=not is_top,
                    base_radius=base,
                    default=sy,
                    col=src_sec.grid_col if src_sec is not None else None,
                )
                if row is not None
                else cy_base
            )
            cy_entry = (
                header_corridor_y(
                    graph,
                    tgt_sec.grid_row,
                    below=not entry_above,
                    base_radius=base,
                    default=ty,
                )
                if tgt_sec is not None
                else (ty - base if entry_above else ty + base)
            )
            centerline = [
                (sx, sy),
                (sx, cy_down),
                (gap_x, cy_down),
                (gap_x, cy_entry),
                (tx, cy_entry),
                (tx, ty),
            ]
        else:
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
        gap_x = inter_col_gap_x()
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


def _top_entry_side_fan_traverse_is_clear(
    edge: Edge, src: Station, tgt: Station, final_x: float, ctx: _RoutingCtx
) -> bool:
    """Whether a below-side fan branch can traverse at the source Y then drop.

    When a junction fans one line to two TOP entries -- one directly below it,
    one below-and-to-the-side -- a drop-first route into the side entry descends
    in a fan lane beside the aligned sibling's straight drop: two same-line
    verticals a bundle-width apart, which trips the parallel-descent guard.
    Traversing at the source Y to the port column and dropping straight in
    removes the shared descent, provided both legs clear every other section.
    """
    graph = ctx.graph
    if edge.source not in graph.junction_ids:
        return False
    if abs(tgt.x - src.x) <= ctx.curve_radius:
        return False  # this branch is itself the aligned drop
    aligned_sibling = any(
        sib.line_id == edge.line_id
        and sib.target != edge.target
        and (sib_port := graph.ports.get(sib.target)) is not None
        and sib_port.side in (PortSide.TOP, PortSide.BOTTOM)
        and (sib_tgt := graph.stations.get(sib.target)) is not None
        and abs(sib_tgt.x - src.x) <= ctx.curve_radius
        for sib in graph.edges_from(edge.source)
    )
    if not aligned_sibling:
        return False
    exclude = {sid for sid in (src.section_id, tgt.section_id) if sid is not None}
    if _h_segment_crosses_other_section(graph, src.x, final_x, src.y, exclude):
        return False
    return not _v_segment_crosses_other_section(graph, final_x, src.y, tgt.y, exclude)


def _corridor_riser_x(
    graph: MetroGraph, src_sec: Section | None, tgt_sec: Section | None
) -> float | None:
    """X mid-way in the clear inter-column gap between two same-row sections.

    A TOP-entry lead-in from a same-row horizontal exit rises through the gap
    between the source and target boxes.  The minimal lead-in (one curve radius
    off the exit port) seats that riser hard against the source box's exit edge;
    centring it in the gap keeps it clear of both walls.  Returns ``None`` when
    the two sections are not a same-row pair, so the caller keeps the lead-in.
    """
    if src_sec is None or tgt_sec is None or src_sec.grid_row != tgt_sec.grid_row:
        return None
    return column_gap_midpoint(
        graph, src_sec.grid_col, tgt_sec.grid_col, row=src_sec.grid_row
    )


def _deepest_section_bottom_crossed_by_run(
    graph: MetroGraph, x1: float, x2: float, y: float, exclude: set[str]
) -> float | None:
    """Lowest bbox bottom among sections a horizontal run at *y* penetrates.

    Scans the sections whose bbox interior the segment ``[min(x1,x2),
    max(x1,x2)]`` at height *y* enters (excluding *exclude*), returning the
    maximum of their bottom edges, or ``None`` when the run crosses no
    section.  A caller drops the run below that edge so it clears the box
    body instead of skimming its interior.
    """
    lo_x, hi_x = (x1, x2) if x1 <= x2 else (x2, x1)
    deepest = float("-inf")
    for s in graph.sections.values():
        if s.bbox_w <= 0 or s.id in exclude:
            continue
        if hi_x <= s.bbox_x or lo_x >= s.bbox_x + s.bbox_w:
            continue
        if s.bbox_y <= y <= s.bbox_y + s.bbox_h:
            deepest = max(deepest, s.bbox_y + s.bbox_h)
    return deepest if deepest > float("-inf") else None


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
    # junction sources.  A junction fed straight from directly above carries no
    # horizontal travel, so its drop stays in the column with no lead-in: a jog
    # there would reverse lateral direction at the entry boundary.
    exit_side = _source_exit_side(ctx.graph, src)
    straight_drop = False
    if exit_side is not None:
        lead = exit_side
    elif abs(dx) > ctx.curve_radius:
        lead = horizontal_direction(dx)
    else:
        lead = Direction.R
        if src.id in ctx.graph.junctions:
            for je in ctx.graph.edges_to(src.id):
                js = ctx.graph.stations.get(je.source)
                if js and js.is_port:
                    if abs(js.x - src.x) <= COORD_TOLERANCE:
                        straight_drop = True
                    else:
                        lead = Direction.R if js.x < src.x else Direction.L
                    break

    lx0 = sx if straight_drop else sx + lead.sign * ctx.curve_radius

    # A same-row horizontal exit whose minimal lead-in would seat the vertical
    # trunk hard against the source box's exit edge runs the riser up that edge.
    # Seat the riser midway in the clear inter-column corridor instead.
    if exit_side is not None and not straight_drop:
        corridor_x = _corridor_riser_x(ctx.graph, src_sec, tgt_sec)
        if corridor_x is not None:
            lx0 = corridor_x

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
    # A section entered from more than one side re-slots its perp entry port
    # from the lines that port alone carries, independently of the feeder, so
    # the multi-side branches below diverge from the single-side behaviour that
    # every existing render relies on.
    multi_side_entry = tgt_sec is not None and (
        len(
            {
                ctx.graph.ports[pid].side
                for pid in tgt_sec.entry_ports
                if pid in ctx.graph.ports
            }
        )
        > 1
    )

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

        # The re-slotted port offset differs from the feeder's, so land on the
        # port's own offset for a multi-side entry; a single-side entry keeps
        # the feeder offset the propagation already matched to the port.
        def tgt_offset(line_id: str) -> float:
            if multi_side_entry:
                return _get_offset(ctx, edge.target, line_id)
            return src_offset(line_id)

        members = [
            (edge_by_line[lid], lid, src_offset(lid), tgt_offset(lid))
            for lid in line_ids
        ]

    # The trunk leg runs horizontally at ``mid_y`` from the lead-in to the
    # landing column.  When a squeezed inter-row gap seats it inside an
    # intervening section (a tall upstream box protruding into the gap the run
    # doubles back across), drop it below that box's bottom edge so it routes
    # in the gap rather than skimming the section interior (#1312).  Clamp
    # above the target's top so the leg still descends into the TOP port.
    exclude = {sid for sid in (src.section_id, tgt.section_id) if sid is not None}
    crossed_bottom = _deepest_section_bottom_crossed_by_run(
        ctx.graph, lx0, final_x, mid_y, exclude
    )
    if crossed_bottom is not None:
        mid_y = min(crossed_bottom + INTER_ROW_EDGE_CLEARANCE, ty - ctx.curve_radius)

    # Traverse at the source Y to the port column, then drop straight in.
    if _top_entry_side_fan_traverse_is_clear(edge, src, tgt, final_x, ctx):
        centerline = [(sx, sy), (final_x, sy), (final_x, ty)]
        transition_leg = 1
    elif not multi_side_entry:
        # Single-side entry keeps the original lead-in shapes: a short lead-in
        # that lands within a corner radius of the column jogs to it at the
        # boundary; otherwise the lateral return runs in the inter-row gap.
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
    elif abs(lx0 - final_x) <= COORD_TOLERANCE:
        # Multi-side entry: the lead-in already sits on the landing column, so
        # drop straight from it.
        centerline = [(sx, sy), (lx0, sy), (lx0, ty)]
        transition_leg = 2
    else:
        # Multi-side entry: return to the landing column in the inter-row gap,
        # above the entry boundary, so the drop enters the port straight rather
        # than reversing laterally at the boundary.
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
    """Drop a LEFT exit into a LEFT entry below it (same column or to the left).

    Both ports sit on a left edge and both face outward to the left, so the
    line leads out leftward, drops vertically in a channel clear of every box,
    then comes back in to the target's left entry port::

        (sx,sy) -> (vx,sy) -> (vx,ty) -> (tx,ty)

    The channel ``vx`` is placed just left of the leftmost of the two columns'
    left edges, so the connector never re-enters either section's bbox -- in
    particular it never claws back across the source box to reach a target that
    sits below and to the left (a folded TB bridge feeding a convergence sink).
    """
    src_col = _resolve_section_col(ctx.graph, src)
    tgt_col = _resolve_section_col(ctx.graph, tgt)
    left_edge = min(
        col_left_edge(ctx.graph, src_col, default=src.x),
        col_left_edge(ctx.graph, tgt_col, default=tgt.x),
    )
    channel_x = min(left_edge, src.x, tgt.x) - ctx.curve_radius - ctx.offset_step

    route = route_hvh_tapered(
        ctx, edge, src, tgt, channel_x, base_radius=ctx.curve_radius
    )
    # When the target sits one or more columns to the left, the descent runs in
    # an inter-column gap and must declare its slot; a same-column drop hugs the
    # column's left edge (in no gap), where this declares nothing.
    _declare_channel(route, ctx, channel_x, vertical_direction(tgt.y - src.y))
    return route


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
    normalize_exempt: bool = True,
    source_leads_down: bool = False,
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

    A trailing perp (TOP/BOTTOM) exit leaves along the flow, not sideways, so
    *source_leads_down* drops the horizontal lead-in: the loop starts with the
    vertical run down the exit column (*corner_x*), collapsing the 6-point loop
    to a 5-point D-H-?-H shape.  The source stagger then rides the drop's normal
    (X) rather than the lead-in's (Y).
    """
    sx, sy = src.x, src.y
    ex, ey = entry_port.x, entry_port.y
    entry_delta = delta if entry_side is PortSide.LEFT else -delta
    src_off = _get_offset(ctx, edge.source, edge.line_id)
    tgt_off = _get_offset(ctx, edge.target, edge.line_id)
    if source_leads_down:
        centerline = [
            (corner_x + src_off + delta, sy),
            (corner_x + src_off + delta, channel_y),
            (descent_x, channel_y),
            (descent_x, ey + tgt_off + entry_delta),
            (ex, ey + tgt_off + entry_delta),
        ]
    else:
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
        normalize_exempt=normalize_exempt,
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
    # Lead-out and LEFT-entry lead-in both run rightward, so port-offset stacking
    # fixes the concentric order regardless of riser direction; force the DOWN
    # (rightward-run) stagger so the body nests into both baked endpoints. ``dy``
    # only picks the channel Y below.
    fan, pos_n, delta, corner_x = _wrap_fan_geometry(ctx, edge, src, i, n, Direction.D)

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

    route = _route_entry_wrap(
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
    _declare_channel(route, ctx, vx, vertical_direction(ty - hy))
    return route


def _route_perp_exit_farside_entry_wrap(f: _InterFacts) -> RoutedPath | None:
    """Wrap a trailing perp (BOTTOM/TOP) exit into a far-side LEFT/RIGHT entry.

    Mirrors :func:`_route_left_entry_wrap` / :func:`_route_right_entry_wrap` but
    leaves the source straight along the flow (``source_leads_down``): the perp
    exit sits on the section's trailing edge, so the loop opens with the vertical
    drop down the exit column into the inter-row gap, then wraps across to a
    channel clear of the target box and approaches the port horizontally from its
    own outward side.
    """
    edge, src, tgt, ctx = f.edge, f.src, f.tgt, f.ctx
    entry_side = f.entry_side
    assert entry_side in (PortSide.LEFT, PortSide.RIGHT)
    sy, ty = src.y, tgt.y
    dy = ty - sy
    _fan, pos_n, delta, _corner_x = _wrap_fan_geometry(
        ctx, edge, src, f.i, f.n, vertical_direction(dy)
    )
    # The perp exit leaves along the flow, so the source-side corner sits on the
    # exit column rather than a lead-out right of the source box.
    corner_x = src.x
    hy = inter_row_channel_y(ctx.graph, src, tgt, sy, ty, dy, ctx.curve_radius, delta)
    hy -= delta
    # The channel leaves the source's trailing edge, so hold it a clear band off
    # that edge (a multi-row midpoint can land closer than the inter-row edge
    # clearance), then keep both vertical legs long enough for the corner curves.
    drop_sign = 1.0 if dy >= 0 else -1.0
    hy = sy + drop_sign * max((hy - sy) * drop_sign, INTER_ROW_EDGE_CLEARANCE)
    lo, hi = (sy, ty) if dy >= 0 else (ty, sy)
    hy = min(max(hy, lo + ctx.curve_radius), hi - ctx.curve_radius)
    if entry_side is PortSide.LEFT:
        vx = _left_entry_descent_x(ctx, tgt.x, pos_n)
    else:
        vx = _right_entry_descent_x(ctx, tgt.x, pos_n)
    route = _route_entry_wrap(
        edge,
        src,
        tgt,
        ctx,
        pos_n=pos_n,
        delta=delta,
        corner_x=corner_x,
        channel_y=hy,
        descent_x=vx,
        entry_side=entry_side,
        source_leads_down=True,
    )
    _declare_channel(route, ctx, vx, vertical_direction(ty - hy))
    return route


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
    required = inter_row_wrap_band(len(bundle_lines), ctx.offset_step)
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

    route = _route_entry_wrap(
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
    _declare_channel(route, ctx, vx, vertical_direction(entry_port.y - gy_base))
    return route


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


def _approach_blocker_right_edge(ctx: _RoutingCtx, entry_port: Station) -> float | None:
    """Right edge of the nearest section blocking a LEFT entry's approach.

    A LEFT entry is reached by a horizontal run at the entry Y from its own
    side.  Any other section whose box straddles that Y and lies left of the
    port sits across that run; the rightmost such box (the immediate blocker) is
    the edge the descent channel must clear.  Returns its right edge, or
    ``None`` when nothing straddles the entry Y.
    """
    ey, ex = entry_port.y, entry_port.x
    own = entry_port.section_id
    best: float | None = None
    for sid, s in ctx.graph.sections.items():
        if sid == own or s.bbox_w <= 0:
            continue
        right = s.bbox_x + s.bbox_w
        if (
            s.bbox_y - COORD_TOLERANCE <= ey <= s.bbox_y + s.bbox_h + COORD_TOLERANCE
            and right < ex - COORD_TOLERANCE
            and (best is None or right > best)
        ):
            best = right
    return best


def _route_around_section_below(
    edge: Edge,
    src: Station,
    tgt: Station,
    entry_port: Station,
    i: int,
    n: int,
    ctx: _RoutingCtx,
    channel_y: float | None = None,
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

    *channel_y* overrides the leftward traverse Y.  A merge trunk reaching a
    leftmost target passes its ``bypass_bottom_y`` channel (the inter-row gap
    its converging branches drop onto) so the trunk runs left at that shared Y
    and descends on the target's far side, rather than diving to the canvas
    bottom where the branches could not meet it.
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
    by = (
        channel_y
        if channel_y is not None
        else bypass_bottom_y(
            ctx.graph,
            bc_src_col,
            bc_tgt_col,
            BYPASS_CLEARANCE,
            src_row=src_row,
            cross_row=True,
        )
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
        # ``column_gap_edges`` spans an empty intervening grid column back to the
        # canvas origin, seating the V_up far left of the real neighbour so the
        # entry-Y approach still crosses it.  Float the V_up right of any section
        # straddling the entry Y (a neighbour whose box grew into this row),
        # capped left of the target so the U-turn into the LEFT port stays valid.
        blocker_right = _approach_blocker_right_edge(ctx, entry_port)
        if blocker_right is not None:
            vx = max(
                vx,
                min(
                    blocker_right + ctx.curve_radius,
                    _left_entry_descent_x(ctx, section_left, pos_n),
                ),
            )
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
    route = _route_entry_wrap(
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
    _declare_channel(route, ctx, vx, vertical_direction(ey - by))
    return route


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
    internal order (driven from the arrival bundle by
    :func:`offsets._reorder_reconvergence`).

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
    route = route_along(edge, members, centerline, base_radius=ctx.curve_radius)
    _declare_channel(route, ctx, descent_x, vertical_direction(mid_ey - channel_y))
    return route


def _leadout_self_meets_sibling_descent(
    ctx: _RoutingCtx,
    edge: Edge,
    corner_x: float,
    y_lo: float,
    y_hi: float,
    gap_right: float,
) -> bool:
    """Whether a same-line descent already sits in this wrap's lead-out band.

    The wrap turns down at ``corner_x`` into the gap between the source column
    and the next.  A descent of the SAME line from a DIFFERENT source, already
    routed down that same gap (``corner_x <= x <= gap_right``) across the drop's
    Y span, would render as one merged corner with this lead-out.  When one is
    there the caller carries the horizontal on and turns down clear to its right.
    """
    lo, hi = (y_lo, y_hi) if y_lo <= y_hi else (y_hi, y_lo)
    for route in ctx.built_routes:
        if not route.is_inter_section or route.line_id != edge.line_id:
            continue
        if route.edge.source == edge.source:
            continue
        for _k, x, seg_lo, seg_hi, _down in iter_vertical_segments(route):
            if not (corner_x - COORD_TOLERANCE <= x <= gap_right + COORD_TOLERANCE):
                continue
            if min(hi, seg_hi) - max(lo, seg_lo) > COORD_TOLERANCE:
                return True
    return False


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

    # A same-line descent from another source already in the lead-out gap would
    # merge with a source-hugging turn-down into one corner.  Carry the
    # horizontal on and turn down clear to its right (bounded at the target row
    # so the drop misses the descent but never reaches a right-column section).
    _gap_left, gap_right = column_gap_edges(
        ctx.graph, src_col, src_col + 1, row=tgt_row
    )
    if _leadout_self_meets_sibling_descent(ctx, edge, corner_x, sy, hy, gap_right):
        corner_x = max(corner_x, gap_right - ctx.curve_radius - ctx.offset_step)

    # V2 descent channel centre, just past the entry port in the gap to the
    # right of the target column.
    vx = _right_entry_descent_x(ctx, tx, pos_n)

    # Same-column source (stacked directly above) drops straight down the
    # corridor when clear, leading to it at the top corner rather than down the
    # wrap's inter-row staging channel.  An adjacent-column source keeps the wrap
    # so its band traverse runs through the inter-row channel between the boxes.
    if src_col == tgt_col and _right_entry_corridor_drop_in_is_clear(
        ctx.graph, src, tgt, vx
    ):
        return _route_right_entry_drop_in(
            edge, src, tgt, ctx, pos_n=pos_n, delta=delta, corner_x=vx
        )

    route = _route_entry_wrap(
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
    route.declare_gap_slot(
        lo_col=tgt_col,
        hi_col=tgt_col + 1,
        row=tgt_row,
        direction=vertical_direction(ty - hy),
        slot_index=0,
        n_slots=1,
    )
    return route


def _right_entry_gap_above_target_y(
    graph: MetroGraph, tgt_row: int
) -> tuple[float, float]:
    """Return ``(gap_top, gap_bottom)`` of the inter-row band ABOVE *tgt_row*.

    The band sits between the row above the target's bottom edge and the
    target row's top edge -- the same band the counter-flow guard checks, so
    the route runs its rightward traverse just above the target row then drops
    into the RIGHT port.  For a source exactly one row up this is the band just
    below the source row; for a source further up it is the wider band the
    intervening rows leave abutting the target, which is what admits the
    with-flow approach when the source-adjacent band is too narrow.  Computed
    over all columns (not column-restricted) so the traverse stays clear of
    every section in the span.
    """
    gap_top = row_bottom_edge(graph, tgt_row - 1)
    gap_bottom = row_top_edge(graph, tgt_row, default=gap_top)
    return gap_top, gap_bottom


def _right_entry_gap_above_is_clear(
    graph: MetroGraph,
    src: Station,
    tgt: Station,
    entry_port: Station,
    tgt_row: int,
) -> bool:
    """Whether a RIGHT-entry feed from above can use the inter-row gap.

    The route runs its long horizontal in the band just above the target
    row, then drops straight down the RIGHT side of the target column into
    the port.  Viable only when that band genuinely exists (the row above the
    target's bottom is above the target row's top), is wide enough for the
    traverse to clear both the upper row's bottom edge and the target row's
    header badge, and the horizontal at the band's centre crosses no section
    interior between the source and the target's right edge.
    """
    gap_top, gap_bottom = _right_entry_gap_above_target_y(graph, tgt_row)
    if gap_bottom <= gap_top:
        return False
    # A band too narrow for both clearances makes the centred run graze the
    # source box bottom, so the feed loops around below the target row instead.
    if not _inter_row_band_fits(gap_top, gap_bottom):
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
    normalize_exempt: bool = True,
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
    route = _route_entry_wrap(
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
        normalize_exempt=normalize_exempt,
    )
    _declare_channel(route, ctx, vx, vertical_direction(entry_port.y - channel_y_base))
    if not normalize_exempt:
        # Open to the gap-bundle pass: its source-side lead-in also drops
        # through an inter-column gap, so declare that channel too or the
        # always-on gap-channel guard flags it as unmaterialised.
        _declare_channel(
            route, ctx, corner_x, vertical_direction(channel_y_base - src.y)
        )
    return route


def _route_right_entry_via_gap_above(
    edge: Edge,
    src: Station,
    tgt: Station,
    entry_port: Station,
    i: int,
    n: int,
    ctx: _RoutingCtx,
    tgt_row: int,
) -> RoutedPath:
    """Route to a RIGHT entry port via the inter-row gap ABOVE the target row.

    Used when the source sits in a row ABOVE the target's row.  Going UNDER
    the whole target row (:func:`_route_right_entry_around_below`) would run
    the long rightward horizontal counter to the target row's flow.  Instead
    run that horizontal in the clear inter-row band just above the target
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
    gap_top, gap_bottom = _right_entry_gap_above_target_y(ctx.graph, tgt_row)
    channel_y_base = _center_inter_row_channel(gap_top, gap_bottom)
    # When two or more distinct lines converge into this one RIGHT entry port,
    # each independently picks the same descent X just right of the target
    # column and they overlay.  Open those to the gap-bundle pass so the
    # same-gap descents spread into concentric slots.  A lone feeder has nothing
    # to spread against, so it stays handler-owned (a normalize restack would
    # only re-shape its self-contained loop).
    converging = len({e.line_id for e in ctx.graph.edges_to(entry_port.id)}) > 1
    return _build_right_entry_wrap_route(
        edge,
        src,
        entry_port,
        i,
        n,
        ctx,
        channel_y_base,
        normalize_exempt=not converging,
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
