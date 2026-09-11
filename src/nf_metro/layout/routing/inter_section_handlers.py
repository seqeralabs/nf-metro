"""Inter-section edge routing: bypass, entry wraps, around-section,
inter-row corridors, stepped descent, and L-shape handlers.

``_route_inter_section`` selects the shape via a declarative table
(``_INTER_SECTION_RULES``): one :class:`_InterFacts` snapshot of the edge's
geometry and topology is matched against named, pairwise-disjoint rules.
The claim space is documented in ``docs/dev/inter_section_dispatch.mdx``.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from functools import cached_property
from math import inf
from types import MappingProxyType
from typing import TYPE_CHECKING, AbstractSet, NamedTuple

from nf_metro.layout.route_plan import (
    ExitTurnDisposition,
    FanPlanDisposition,
    FanRouteEmitter,
)
from nf_metro.layout.routing.families import RouteFamilyId

if TYPE_CHECKING:
    from nf_metro.layout.route_plan import RoutePlanObserver

from nf_metro.layout.constants import (
    BYPASS_CLEARANCE,
    COORD_TOLERANCE,
    COORD_TOLERANCE_FINE,
    EDGE_TO_BUNDLE_CLEARANCE,
    INTER_ROW_EDGE_CLEARANCE,
    INTER_ROW_HEADER_CLEARANCE,
    MERGE_ROUTE_MARGIN,
    NEXT_ROW_HEADER_BADGE_CLEARANCE,
    SECTION_ROUTE_CLEARANCE,
)
from nf_metro.layout.geometry import cotravelling_lane_clearance, lanes_run_along_x
from nf_metro.layout.pass_metrics import canvas_edge_clearance
from nf_metro.layout.routing.bundle import build_tapered_bundle
from nf_metro.layout.routing.centrelines import (
    _Member,
    _TaperedMember,
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
    route_vhvh_offset,
)
from nf_metro.layout.routing.common import (
    Direction,
    RoutedPath,
    _center_inter_row_channel,
    _inter_row_band_fits,
    _v_segment_crosses_other_section,
    bundle_width,
    bypass_bottom_y,
    centre_inter_column_channel,
    clear_channel_of_section_edge,
    col_left_edge,
    col_right_edge,
    column_gap_edges,
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
    lowest_section_bottom_crossing_span,
    max_grid_row_with_content,
    merge_trunk_force_cross_row,
    needs_perp_approach_fan,
    packed_cell_neighbor_edges,
    reserved_row_band_between,
    resolve_section,
    right_normal_axis_sign,
    row_bottom_edge,
    row_top_edge,
    section_header_top,
    section_ids_of_stations,
    segment_direction,
    symmetric_bundle_midpoint,
    trailing_perp_side,
    vertical_direction,
)
from nf_metro.layout.routing.context import (
    HopEnd,
    _get_offset,
    _has_intervening_sections,
    _hop_needs_bypass,
    _resolve_section_col,
    _resolve_section_colrow,
    _RoutingCtx,
    _tb_x_offset,
    is_near_vertical_drop,
)
from nf_metro.layout.routing.corners import (
    bypass_stagger,
    l_shape_stagger,
    outer_lane_radius,
)
from nf_metro.layout.routing.normalize import (
    _clear_channel_x_in_band,
    _gap_channel_base,
    _h_segment_crosses_other_section,
    _restack_channel,
    _VChannel,
)
from nf_metro.layout.routing.perp import (
    _perp_approach_fan_x,
    _perp_entry_crossing_x,
    _perp_riser_lateral,
)
from nf_metro.layout.routing.reserved_bands import (
    EdgeKey,
    ReservedBand,
    corridor_clearance_band,
    held_in_reserved_band,
    seat_bundle_in_claimed_bands,
    seat_bundle_in_corridor_clearance,
    seat_run_in_corridor_clearance,
)
from nf_metro.parser.model import (
    Edge,
    MetroGraph,
    Port,
    PortSide,
    Section,
    Station,
)
from nf_metro.parser.route_topology import ResolvedEdge


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
    cellmate_blocks_source_row: bool
    cellmate_blocks_target_row: bool
    merge_ep: Station | None

    @property
    def graph(self) -> MetroGraph:
        return self.ctx.graph

    @cached_property
    def bypass_route(self) -> _BypassRoute:
        return _bypass_route_kind(self)

    @cached_property
    def bottom_exit_junction_route(self) -> _BottomExitJunctionRoute:
        return _bottom_exit_junction_route_kind(self)

    @cached_property
    def merge_entry_route(self) -> _MergeEntryRoute:
        return _merge_entry_route_kind(self)

    @cached_property
    def merge_trunk_shape(self) -> _MergeTrunkShape:
        return _merge_trunk_shape(self)

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

    def section_colrow(self, station: Station) -> tuple[int | None, int | None]:
        cached = {
            self.src.id: (self.src_col, self.src_row),
            self.tgt.id: (self.tgt_col, self.tgt_row),
        }.get(station.id)
        return cached or _resolve_section_colrow(self.graph, station)

    def h_segment_crosses_other_section(
        self,
        x1: float,
        x2: float,
        y: float,
        exclude: AbstractSet[str] | None = None,
        margin: float = 0.0,
    ) -> bool:
        return _h_segment_crosses_other_section(
            self.graph,
            x1,
            x2,
            y,
            self.endpoint_section_ids if exclude is None else exclude,
            margin,
        )

    def v_segment_crosses_other_section(
        self,
        x: float,
        y1: float,
        y2: float,
        exclude: AbstractSet[str] | None = None,
        margin: float = 0.0,
    ) -> bool:
        return _v_segment_crosses_other_section(
            self.graph,
            x,
            y1,
            y2,
            self.endpoint_section_ids if exclude is None else exclude,
            margin,
        )

    @cached_property
    def endpoint_section_ids(self) -> frozenset[str]:
        return frozenset(section_ids_of_stations(self.graph, self.src, self.tgt))

    @cached_property
    def canonical_family_id(self) -> RouteFamilyId | None:
        return next(
            (claim.family_id for claim in _INTER_SECTION_CLAIMS if claim.when(self)),
            None,
        )

    @property
    def entry_side(self) -> PortSide | None:
        """The target entry port's side, or ``None`` when the target is not one."""
        if self.tgt_port is not None and self.tgt_port.is_entry:
            return self.tgt_port.side
        return None

    @property
    def effective_entry_side(self) -> PortSide | None:
        """Side of the entry port the hop ultimately lands on.

        A merge junction is a virtual target standing in front of a real entry
        port, so a hop into one lands on that port's side; a hop into a real
        entry port lands on its own.  ``None`` when the target carries no side.
        """
        if self.merge_ep is not None:
            return self.graph.ports[self.merge_ep.id].side
        return self.entry_side

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
        inter-column gap instead (:func:`_route_around_stack`).
        """
        if not self.is_tb_bottom_exit:
            return False
        return self.v_segment_crosses_other_section(self.sx, self.sy, self.ty)

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

        The source leaves through a TOP/BOTTOM edge: perpendicular to a
        horizontal flow, or along the trailing edge of a vertical flow. The
        consumer is a LEFT/RIGHT entry whose port faces away from the source
        (:attr:`left_entry_from_right` / its right-side mirror). The ordinary
        perpendicular-exit route would descend on the target's near side and
        cross its interior to reach the far-edge port; the clean shape continues
        through the gap, then wraps around the target to approach the port from
        its outward side.

        Unlike :attr:`is_tb_bottom_exit` this is offset-independent, so the
        validate and render routing paths dispatch it identically.
        """
        if (
            self.src_port is None
            or self.src_port.is_entry
            or self.src.section_id is None
        ):
            return False
        section = self.graph.sections.get(self.src.section_id)
        if section is None:
            return False
        leaves_through_perp_edge = self.is_perp_exit or (
            self.src.section_id in self.ctx.tb_sections
            and self.src_port.side == trailing_perp_side(section.direction)
        )
        return (
            self.cross_row
            and (self.left_entry_from_right or self.right_entry_from_left)
            and leaves_through_perp_edge
        )

    @property
    def is_merge_trunk(self) -> bool:
        """Source carries the full bypass trunk of its merge junction."""
        return self.ctx.merge.trunk_source.get(self.edge.target) == self.edge.source

    @property
    def is_merge_branch(self) -> bool:
        """Source is a feeder classified onto its merge trunk's bypass channel.

        Only the trunk carries the full route to the entry port; a branch stops
        on the trunk's channel so the converging line stays a single stroke.
        ``_classify_merge_edges`` owns the choice, since the merge -> entry hop
        it keeps or drops has to agree with which feeders end short.
        """
        return (
            self.edge.source,
            self.edge.target,
            self.edge.line_id,
        ) in self.ctx.merge.branch_edges

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

    @property
    def is_right_exit(self) -> bool:
        """Source is a RIGHT-side exit port (not an entry)."""
        return (
            self.src_port is not None
            and not self.src_port.is_entry
            and self.src_port.side is PortSide.RIGHT
        )

    @property
    def is_stacked_right_exit_right_entry(self) -> bool:
        """RIGHT exit dropping into a RIGHT entry stacked in the same column.

        Both ports pin to the column's shared right edge, so they land at the
        same X.  The RIGHT-side reflection of
        :attr:`is_serpentine_left_exit_left_entry`, routed through the
        ``RIGHT entry wrap`` rule so the drop bows out past the port's outward
        edge (see that rule and ``check_stacked_right_ports_bow_out``).
        """
        return (
            self.is_right_exit
            and self.entry_side is PortSide.RIGHT
            and self.same_col
            and self.cross_row
        )


def _build_inter_facts(
    edge: Edge, src: Station, tgt: Station, ctx: _RoutingCtx
) -> _InterFacts:
    graph = ctx.graph
    src_col, src_row = _resolve_section_colrow(graph, src)
    tgt_col, tgt_row = _resolve_section_colrow(graph, tgt)
    bypass = _hop_needs_bypass(
        graph, HopEnd(src, src_col, src_row), HopEnd(tgt, tgt_col, tgt_row)
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
        needs_bypass=bypass.needed,
        cellmate_blocks_source_row=bypass.cellmate_blocks_source_row,
        cellmate_blocks_target_row=bypass.cellmate_blocks_target_row,
        merge_ep=graph.stations.get(ep_id) if ep_id else None,
    )


def _route_planned_lane_transition(
    edge: Edge,
    ctx: _RoutingCtx,
    *,
    is_inter_section: bool,
) -> RoutedPath | None:
    from nf_metro.layout.routing.exit_turns import route_planned_lane_transition

    return route_planned_lane_transition(
        edge,
        ctx,
        is_inter_section=is_inter_section,
    )


def _route_straight_connector(f: _InterFacts) -> RoutedPath | None:
    """Straight horizontal (same Y) or vertical (same X) connector."""
    planned_transition = _route_planned_lane_transition(
        f.edge,
        f.ctx,
        is_inter_section=True,
    )
    if planned_transition is not None:
        return planned_transition
    return route_straight(
        f.edge, f.ctx, (f.sx, f.sy), (f.tx, f.ty), base_radius=f.ctx.curve_radius
    )


def _route_near_vertical_junction(f: _InterFacts) -> RoutedPath | None:
    """Drop a same-column junction into its entry via the inter-column gap.

    A standard L-shape would place the vertical channel toward the target (back
    inside the shared column); push it the other way so the line keeps the
    junction's natural direction before dropping.  The column is a starting
    guess: it is declared as a gap slot, and :func:`_materialize_gap_slots`
    re-ranks it against every other leg descending the same gap.
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
    if f.merge_trunk_shape.around_below:
        return _route_merge_trunk_around_below(f)
    return _route_merge_trunk(f, f.merge_trunk_shape)


def _route_merge_branch_feeder(f: _InterFacts) -> RoutedPath | None:
    """Dispatch wrapper: a non-trunk feeder's descent onto the trunk channel."""
    assert f.src_col is not None
    return _route_merge_branch(f.edge, f.src, f.ctx, f.src_col)


class _BypassRoute(Enum):
    """Leaf selected for a multi-column bypass hop."""

    L_SHAPE = "l_shape"
    CELLMATE_GAP_DROP = "cellmate_gap_drop"
    LEFT_ENTRY_FAMILY = "left_entry_family"
    RIGHT_ENTRY_CROSS_ROW = "right_entry_cross_row"
    LEFT_EXIT_AROUND_BELOW = "left_exit_around_below"
    PACKED_CELL_SAME_ROW = "packed_cell_same_row"
    U_BYPASS = "u_bypass"


class _BottomExitJunctionRoute(Enum):
    """The named shape emitted from a bottom-exit junction."""

    FAN_LANDINGS = "fan_landings"
    VIA_GAP = "via_gap"
    PLAIN = "plain"


def _bypass_route_kind(f: _InterFacts) -> _BypassRoute:
    """Classify a multi-column hop past intervening sections (``needs_bypass``).

    A LEFT entry one row directly below drops straight in when the entry-Y
    horizontal is clear (no canvas-bottom loop); a RIGHT entry fed from the left
    wraps around its outward side (via the inter-row gap above when clear, else
    the around-below loop); a far-side LEFT entry fed from a LEFT exit to its
    right wraps around below into the port's outward side; everything else takes
    the U-shaped bypass.

    ``CELLMATE_GAP_DROP`` and ``PACKED_CELL_SAME_ROW`` name the two arrangements
    whose leaf is chosen by whether a candidate route can be built at all, so
    which shape they draw is settled at emission rather than here.

    ``held_back_by_boxed_fanout`` qualifies on the source row because the
    band-hop it defers to leads out along that row: the band-hop has nothing
    to offer a hop whose own Y already clears the cell-mate.
    """
    if (
        f.entry_side is PortSide.LEFT
        and f.src_row is not None
        and f.tgt_row is not None
        and f.tgt_row == f.src_row + 1
    ):
        exclude = f.endpoint_section_ids
        if f.cellmate_blocks_source_row and not f.left_entry_from_right:
            # The gap-centred L-shape channel lands past the blocking cell-mate,
            # so test the plain L-shape against its actual vertical channel (both
            # legs) rather than the raw endpoint-to-endpoint span -- a same-column
            # packed-to-packed drop balances its channel in the gap between the
            # two cell-mates and stays clear.
            mid_x = _l_shape_mid_x(f.edge, f.src, f.tgt, f.n, f.ctx)
            if not f.h_segment_crosses_other_section(
                f.sx, mid_x, f.sy, exclude
            ) and not f.h_segment_crosses_other_section(mid_x, f.tx, f.ty, exclude):
                return _BypassRoute.L_SHAPE
            return _BypassRoute.CELLMATE_GAP_DROP
        if not f.left_entry_from_right and not f.h_segment_crosses_other_section(
            f.sx, f.tx, f.ty, exclude
        ):
            return _BypassRoute.L_SHAPE
        if f.left_entry_from_right:
            # Entry-Y blocked: return through the clear inter-row gap as a
            # concentric serpentine wrap.  The below-row U dive cannot fan a
            # bundle leaving a shared exit port (collinear lead-out) and
            # collapses its lines onto one channel.
            return _BypassRoute.LEFT_ENTRY_FAMILY
    if f.right_entry_from_left:
        return _BypassRoute.RIGHT_ENTRY_CROSS_ROW
    if f.left_entry_from_right and f.is_left_exit:
        return _BypassRoute.LEFT_EXIT_AROUND_BELOW
    held_back_by_boxed_fanout = (
        f.cellmate_blocks_source_row and _source_is_boxed_fanout_junction(f)
    )
    if (
        f.entry_side is PortSide.LEFT
        and (f.cellmate_blocks_source_row or f.cellmate_blocks_target_row)
        and f.src_row == f.tgt_row
        and not held_back_by_boxed_fanout
    ):
        return _BypassRoute.PACKED_CELL_SAME_ROW
    if f.left_entry_from_right:
        return _BypassRoute.LEFT_ENTRY_FAMILY
    return _BypassRoute.U_BYPASS


def _route_bypass_cellmate_gap_drop(f: _InterFacts) -> RoutedPath | None:
    """Use the clear cell-mate channel, or the U-bypass when it disappears."""
    return _route_cellmate_gap_drop(f) or _route_bypass(f, _bypass_geometry(f))


def _route_bypass_packed_cell_same_row(f: _InterFacts) -> RoutedPath | None:
    """Reach a packed same-row LEFT entry through its first viable corridor."""
    shared_handoff = _route_packed_cell_same_line_handoff(f)
    if shared_handoff is not None:
        return shared_handoff
    geometry = _left_entry_over_top_geometry(f)
    if geometry is not None:
        return _route_left_entry_over_top(f, geometry)
    return _route_left_entry_family(f)


def _route_u_bypass_family(f: _InterFacts) -> RoutedPath:
    """Build the U-shaped remainder of the bypass family."""
    return _route_bypass(f, _bypass_geometry(f))


def cellmate_gap_drop_column(f: _InterFacts) -> float | None:
    """The cell-mate gap channel a blocked source row descends in.

    When a packed cell-mate blocks the source row, the gap-centred L-shape
    channel lands past the cell-mate and its source-row leg plows through it,
    so the U-bypass would otherwise take over and split the descent into two
    channels joined by a jog.  A cleaner drop exists when the gap between the
    source section and that cell-mate has room for the whole descent: run the
    vertical channel there and turn once along the target row.  Returns
    ``None`` (deferring to the U-bypass) when there is no such cell-mate gap
    or any of the three legs is obstructed.
    """
    src_sec = resolve_section(f.graph, f.src, prefer_upstream=False)
    if src_sec is None:
        return None
    side = PortSide.RIGHT if f.horizontal is Direction.R else PortSide.LEFT
    edges = packed_cell_neighbor_edges(f.graph, src_sec.id, side)
    if edges is None:
        return None
    mid_x = (edges[0] + edges[1]) / 2
    exclude = f.endpoint_section_ids
    if (
        f.h_segment_crosses_other_section(f.sx, mid_x, f.sy, exclude)
        or f.v_segment_crosses_other_section(mid_x, f.sy, f.ty, exclude)
        or f.h_segment_crosses_other_section(mid_x, f.tx, f.ty, exclude)
    ):
        return None
    return mid_x


def _route_cellmate_gap_drop(f: _InterFacts) -> RoutedPath | None:
    """Single-channel L-shape descending the gap before the blocking cell-mate."""
    mid_x = cellmate_gap_drop_column(f)
    if mid_x is None:
        return None
    return _route_l_shape_plain(f.edge, f.src, f.tgt, f.n, f.ctx, mid_x=mid_x)


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


@dataclass(frozen=True, slots=True)
class _SourceSeam:
    """Where a resolved centreline opens: its first run, and the turn off it.

    Every shape a family draws states the same four things about its own start
    -- the direction the member leaves the source on, the direction it turns to,
    the coordinate the run launches from, and the one the turn lands on -- and
    the exit-turn planner reads only those.  A shape whose centreline never
    turns leaves everything but ``run_direction`` unstated.
    """

    run_direction: Direction | None
    turn_direction: Direction | None
    launch_coordinate: float | None
    axis_coordinate: float | None

    @property
    def minimum_runway(self) -> float | None:
        """How far the run travels before the turn, where both are stated."""
        if self.launch_coordinate is None or self.axis_coordinate is None:
            return None
        return abs(self.axis_coordinate - self.launch_coordinate)


@dataclass(frozen=True)
class _LeftExitUnderTargetLoop:
    """The loop a LEFT exit draws to a far-side LEFT entry, plus its source seam.

    ``members`` carries each line's source and target offset against
    ``centreline``; a leg places its member at that offset along the leg's own
    right-hand normal, which is what the seam's ``axis_coordinate`` reads for
    the descent.  ``lane_columns`` is that reading for the whole bundle.
    """

    centreline: tuple[tuple[float, float], ...]
    members: tuple[tuple[Edge, str, float, float], ...]
    lane_columns: tuple[tuple[tuple[str, str, str], float], ...]
    over_top: bool
    corner_x: float
    channel_y: float
    descent_x: float
    seam: _SourceSeam


def left_exit_around_below_geometry(f: _InterFacts) -> _LeftExitUnderTargetLoop:
    """Resolve the loop shared by far-side LEFT-entry planning and emission.

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
    edge, src, tgt, ctx, graph = f.edge, f.src, f.tgt, f.ctx, f.graph
    sx, sy = src.x, src.y
    ex, ey = tgt.x, tgt.y
    _members, line_ids, edge_by_line = gather_member_edges(graph, edge)
    exit_offs = {lid: _get_offset(ctx, edge.source, lid) for lid in line_ids}
    entry_offs = {lid: _get_offset(ctx, edge.target, lid) for lid in line_ids}
    n = len(line_ids)

    src_col, src_row = f.src_col, f.src_row
    tgt_col, tgt_row = f.tgt_col, f.tgt_row
    bw = bundle_width(n, ctx.offset_step)

    # Descent channel in the inter-column gap just LEFT of the source.  The exit
    # taper fans the drop rightward by the exit offset, so the channel's left
    # member is at ``cx`` and the box-near member at ``cx + max(exit_offs)``;
    # shift the gap midline left by half that spread to centre the fan in the
    # gap and keep both flanks clear.
    max_exit = max(exit_offs.values(), default=0.0)
    # No column to the left is the same story as a row that column does not
    # occupy: no gap to centre the descent in.  Both arrive as the degenerate
    # pair :func:`column_gap_edges` returns for an unbounded gap.
    gap_left, gap_right = (
        column_gap_edges(graph, src_col - 1, src_col, row=src_row)
        if src_col is not None and src_col > 0
        else (0.0, 0.0)
    )
    if gap_right > gap_left:
        cx = symmetric_bundle_midpoint(gap_left, gap_right, [bw], 0) - max_exit / 2
        # A wide gap's midpoint can sit far left of the source; keep the descent
        # hugging the source's own left edge so the drop stays beside the box it
        # leaves rather than out in the open span.
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
        reserved=ctx.reserved_bands.rows,
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
    # Run the horizontal over the target's top when viable, else dip below it
    # (see :func:`_left_exit_wrap_over_top_y`).
    over_gy = _left_exit_wrap_over_top_y(f, cx, vx)
    over_top = over_gy is not None
    channel_y = over_gy if over_gy is not None else by
    lead_out = -exit_offs[edge.line_id]
    run_direction = _leg_direction((sx, sy), (cx, sy))
    lead_out_y = sy + lead_out * right_normal_axis_sign(run_direction)
    turn_direction = _leg_direction((cx, lead_out_y), (cx, channel_y))
    return _LeftExitUnderTargetLoop(
        (
            (sx, sy),
            (cx, sy),
            (cx, channel_y),
            (vx, channel_y),
            (vx, ey),
            (ex, ey),
        ),
        tuple(members),
        tuple(
            _wrap_lane_coordinates(
                [(edge_by_line[lid], lid, -exit_offs[lid]) for lid in line_ids],
                cx,
                turn_direction,
            )
        ),
        over_top,
        cx,
        channel_y,
        vx,
        _SourceSeam(
            run_direction,
            turn_direction,
            sx,
            cx + lead_out * right_normal_axis_sign(turn_direction),
        ),
    )


def _route_left_exit_around_below_left_entry(f: _InterFacts) -> RoutedPath | None:
    """Build the loop :func:`left_exit_around_below_geometry` describes."""
    edge, src, tgt, ctx = f.edge, f.src, f.tgt, f.ctx
    geometry = left_exit_around_below_geometry(f)
    # The over-top loop's outward-side port approach reads as a backtrack to the
    # normalize pass, so it opts out; the dip stays normalize-able, letting a
    # multi-feeder port's dips bundle apart into separate descents.
    route = route_tapered(
        edge,
        list(geometry.members),
        list(geometry.centreline),
        transition_leg=3,
        base_radius=ctx.curve_radius,
        normalize_exempt=geometry.over_top,
    )
    if route is not None:
        _declare_channel(
            route,
            ctx,
            geometry.corner_x,
            vertical_direction(geometry.channel_y - src.y),
        )
        _seat_left_exit_under_target_descent(route, edge, geometry, ctx)
        _declare_channel(
            route,
            ctx,
            geometry.descent_x,
            vertical_direction(tgt.y - geometry.channel_y),
        )
    return route


def _left_exit_wrap_over_top_y(
    f: _InterFacts,
    cx: float,
    vx: float,
) -> float | None:
    """Over-the-top channel Y for a same-row LEFT-exit far-LEFT-entry wrap loop.

    A same-row wrap dips below the target box; when the target is tall that dip
    runs far below every section before returning the full width.  Returns the
    centre of the inter-row band ABOVE the target row -- humping the loop over the
    target's top, never descending past the box bottom, mirroring the from-above
    gap-above approach -- when the port is fed by this line alone, that band
    exists, is wide enough for the traverse, and all three loop legs (source-side
    ascent at *cx*, band traverse, target-side descent at *vx*) clear other
    section interiors.  Returns ``None`` to keep the dip otherwise.

    A port fed by more than one line keeps the dip: the band above holds a single
    channel, so pooling every feeder into it collinearly overlays them, whereas
    the dips give each feeder its own descent below the target.
    """
    graph, src, tgt, tgt_row = f.graph, f.src, f.tgt, f.tgt_row
    gap_top, gap_bottom = (
        _gap_above_target_y(graph, tgt_row) if tgt_row is not None else (0.0, 0.0)
    )
    if gap_bottom <= gap_top or not _inter_row_band_fits(gap_top, gap_bottom):
        return None
    if len({e.line_id for e in graph.edges_to(tgt.id)}) > 1:
        return None
    gy = _center_inter_row_channel(
        gap_top, gap_bottom, reserved=f.ctx.reserved_bands.rows.at(tgt_row)
    )
    blocked = (
        f.v_segment_crosses_other_section(cx, src.y, gy)
        or f.v_segment_crosses_other_section(vx, gy, tgt.y)
        or f.h_segment_crosses_other_section(cx, vx, gy)
    )
    return None if blocked else gy


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
    if f.tgt_row is not None and _right_entry_gap_above_is_clear(f):
        return _route_right_entry_via_gap_above(
            edge, src, tgt, f.i, f.n, ctx, f.tgt_row
        )
    return _route_right_entry_around_below(f)


class _LeftEntryRoute(Enum):
    """Which shape :func:`_route_left_entry_family` builds for a LEFT-entry feed."""

    LEFT_EXIT_DROP = "left_exit_drop"
    CORRIDOR = "corridor"
    GAP_ABOVE = "gap_above"
    BAND_HOP = "band_hop"
    AROUND_BELOW = "around_below"
    WRAP = "wrap"


def _left_entry_route_kind(f: _InterFacts) -> _LeftEntryRoute:
    """Classify a cross-row LEFT-entry feed without building the route.

    A LEFT-side exit already faces outward toward the LEFT entry, so it takes
    the ``LEFT_EXIT_DROP`` straight down a channel clear of both boxes; the
    rightward-lead-out ``WRAP`` would claw its leftward channel run back across
    a tall source box (a folded TB bridge feeding a sink below and to the left).

    Everything else wraps leftward through the inter-row gap.  When that gap
    horizontal lands inside an intervening section, the feed descends the clear
    ``CORRIDOR`` if one exists.  Failing that, a source a row (or more) ABOVE
    the target reaches its port through the clear band abutting the target row
    (``GAP_ABOVE``), or, where the source-adjacent band is blocked by a packed
    cell-mate boxing in a fan junction, peels off through a clear descent column
    between the two bands (``BAND_HOP``).  The remaining feeds dive
    ``AROUND_BELOW`` the whole target row and run the full width back.
    """
    src, tgt, ctx, graph = f.src, f.tgt, f.ctx, f.graph
    if f.is_left_exit:
        return _LeftEntryRoute.LEFT_EXIT_DROP
    wrap_hy = inter_row_channel_y(
        graph,
        src,
        tgt,
        f.sy,
        f.ty,
        f.dy,
        ctx.curve_radius,
        reserved=ctx.reserved_bands.rows,
    )
    exclude = {src.section_id} if src.section_id else set[str]()
    if not f.h_segment_crosses_other_section(f.sx, f.tx, wrap_hy, exclude):
        return _LeftEntryRoute.WRAP
    if _corridor_is_viable(ctx, src, tgt):
        return _LeftEntryRoute.CORRIDOR
    if f.src_row is not None and f.tgt_row is not None and f.src_row < f.tgt_row:
        if _left_entry_gap_above_is_clear(f):
            return _LeftEntryRoute.GAP_ABOVE
        if _left_entry_band_hop_is_clear(f):
            return _LeftEntryRoute.BAND_HOP
    return _LeftEntryRoute.AROUND_BELOW


def _route_left_entry_family(f: _InterFacts) -> RoutedPath | None:
    """Cross-row feed into a LEFT entry from a source on its right.

    A standard L-shape would cut through the target interior to reach the
    left-side port.  Each shape the family can take is named by
    :func:`_left_entry_route_kind`, which chooses between them.
    """
    edge, src, tgt, ctx = f.edge, f.src, f.tgt, f.ctx
    kind = _left_entry_route_kind(f)
    if kind is _LeftEntryRoute.LEFT_EXIT_DROP:
        return _route_left_exit_left_entry_drop(edge, src, tgt, ctx)
    if kind is _LeftEntryRoute.CORRIDOR:
        return _route_inter_row_gap_corridor(edge, src, tgt, f.i, f.n, ctx)
    if kind is _LeftEntryRoute.GAP_ABOVE:
        assert f.tgt_row is not None
        return _route_left_entry_via_gap_above(edge, src, tgt, f.i, f.n, ctx, f.tgt_row)
    if kind is _LeftEntryRoute.BAND_HOP:
        return _route_left_entry_via_band_hop(f)
    if kind is _LeftEntryRoute.AROUND_BELOW:
        return _route_around_section_below(edge, src, tgt, tgt, f.i, f.n, ctx)
    return _route_left_entry_wrap(edge, src, tgt, f.i, f.n, ctx)


def _route_left_entry_corridor(f: _InterFacts) -> RoutedPath | None:
    """Build the corridor leaf selected for a cross-row LEFT entry."""
    return _route_inter_row_gap_corridor(f.edge, f.src, f.tgt, f.i, f.n, f.ctx)


def _takes_left_entry_corridor(f: _InterFacts) -> bool:
    """Whether the route reaches the corridor leaf of the LEFT-entry family."""
    direct = f.entry_side is PortSide.LEFT and f.dx < 0 and f.cross_row
    bypass = f.needs_bypass and f.bypass_route is _BypassRoute.LEFT_ENTRY_FAMILY
    return (direct or bypass) and _left_entry_route_kind(f) is _LeftEntryRoute.CORRIDOR


def _packed_cell_target_sibling(f: _InterFacts) -> tuple[Edge, Station, Section] | None:
    """A nearer same-line target packed immediately before this target."""
    target_port = f.graph.ports.get(f.edge.target)
    if target_port is None:
        return None
    target_section = f.graph.section_for_port(target_port)
    pack = next(
        (
            members
            for members in f.graph.cell_packs.values()
            if target_section.id in members
        ),
        None,
    )
    if pack is None:
        return None

    candidates: list[tuple[Edge, Station, Section]] = []
    for edge in f.graph.edges_from(f.edge.source):
        if edge is f.edge or edge.line_id != f.edge.line_id:
            continue
        sibling_port = f.graph.ports.get(edge.target)
        if sibling_port is None or not sibling_port.is_entry:
            continue
        sibling_section = f.graph.section_for_port(sibling_port)
        sibling_station = f.graph.stations[edge.target]
        if (
            sibling_section.id not in pack
            or sibling_section.id == target_section.id
            or sibling_port.side is not target_port.side
            or sibling_section.bbox_x + sibling_section.bbox_w
            > target_section.bbox_x + COORD_TOLERANCE
            or sibling_station.x <= f.src.x + COORD_TOLERANCE
        ):
            continue
        candidates.append((edge, sibling_station, sibling_section))
    if not candidates:
        return None
    return max(candidates, key=lambda candidate: candidate[2].bbox_x)


@dataclass(frozen=True)
class _PackedCellHandoff:
    """A hop leaving the source on a packed sibling's descent, then splitting off.

    ``carrier`` is the sibling whose U-shaped bypass draws the shared opening,
    ``descent`` that bypass's geometry, and ``centreline`` the whole hop: the
    carrier's first four points, then the legs under the sibling's box.
    """

    carrier: Edge
    descent: _BypassGeometry
    centreline: list[tuple[float, float]]


def packed_cell_handoff_carrier(f: _InterFacts) -> tuple[Edge, _BypassGeometry] | None:
    """The packed sibling whose descent this hop leaves the source on.

    ``None`` where the hop draws a corridor of its own, which is what
    :func:`_packed_cell_handoff_plan` refuses the hand-over for.
    """
    plan = _packed_cell_handoff_plan(f)
    return None if plan is None else (plan.carrier, plan.descent)


def _packed_cell_handoff_plan(f: _InterFacts) -> _PackedCellHandoff | None:
    """The carrier this hand-over shares, with the centreline it then draws.

    ``None`` where no sibling carries it, the sibling's own hop is not a plain
    multi-column one, or any leg of the split-off passes through a section.
    """
    sibling = _packed_cell_target_sibling(f)
    if sibling is None:
        return None
    sibling_edge, sibling_target, sibling_section = sibling
    sibling_facts = _build_inter_facts(sibling_edge, f.src, sibling_target, f.ctx)
    if (
        not sibling_facts.needs_bypass
        or sibling_facts.src_col is None
        or sibling_facts.tgt_col is None
        or sibling_facts.cellmate_blocks_source_row
    ):
        return None
    sibling_descent = _bypass_geometry(sibling_facts)
    sibling_route = _route_bypass(sibling_facts, sibling_descent)
    if sibling_route is None or len(sibling_route.points) < 6:
        return None

    prefix = sibling_route.points[:4]
    split_x, split_y = prefix[-1]
    _fan, pos_n, _delta, _corner_x = _wrap_fan_geometry(
        f.ctx, f.edge, f.src, f.i, f.n, Direction.D
    )
    descent_x = _left_entry_descent_x(f.ctx, f.tgt.x, pos_n)
    under_y = sibling_section.bbox_y + sibling_section.bbox_h + BYPASS_CLEARANCE
    target_y = f.tgt.y + _get_offset(f.ctx, f.edge.target, f.edge.line_id)
    exclude = f.endpoint_section_ids
    if (
        f.v_segment_crosses_other_section(split_x, split_y, under_y, exclude)
        or f.h_segment_crosses_other_section(split_x, descent_x, under_y, exclude)
        or f.v_segment_crosses_other_section(descent_x, under_y, target_y, exclude)
    ):
        return None

    return _PackedCellHandoff(
        sibling_edge,
        sibling_descent,
        [
            *prefix,
            (split_x, under_y),
            (descent_x, under_y),
            (descent_x, target_y),
            (f.tgt.x, target_y),
        ],
    )


def _route_packed_cell_same_line_handoff(f: _InterFacts) -> RoutedPath | None:
    """Share the nearer packed sibling's corridor, then pass below its box."""
    plan = _packed_cell_handoff_plan(f)
    if plan is None:
        return None
    centerline = plan.centreline
    ctx = f.ctx
    route = route_along(
        f.edge,
        [(f.edge, f.edge.line_id, 0.0)],
        centerline,
        base_radius=ctx.curve_radius,
        normalize_exempt=False,
    )
    assert route is not None
    _declare_channel(route, ctx, centerline[1][0], Direction.D)
    _declare_channel(route, ctx, centerline[3][0], Direction.D)
    _declare_channel(route, ctx, centerline[5][0], Direction.U)
    # Two of the corridor's columns can fall in one gap, which leaves the third
    # leg holding a gap no targeted declaration named.
    _declare_placed_channels(route, ctx)
    return route


def _left_entry_over_top_geometry(
    f: _InterFacts,
) -> _EntryWrapGeometry | None:
    """Resolve a clear row-top corridor for a same-row packed-cell bypass."""
    assert f.tgt_row is not None
    graph, src, tgt, ctx = f.graph, f.src, f.tgt, f.ctx
    _fan, pos_n, delta, corner_x = _wrap_fan_geometry(
        ctx, f.edge, src, f.i, f.n, Direction.U
    )
    descent_x = _left_entry_descent_x(ctx, tgt.x, pos_n)
    channel_y = header_corridor_y(
        graph,
        f.tgt_row,
        below=False,
        base_radius=ctx.curve_radius,
        default=tgt.y,
    )
    if (
        f.v_segment_crosses_other_section(corner_x, src.y, channel_y)
        or f.h_segment_crosses_other_section(corner_x, descent_x, channel_y)
        or f.v_segment_crosses_other_section(descent_x, channel_y, tgt.y)
    ):
        return None
    span_lo, span_hi = sorted((corner_x, descent_x))
    crossed_header_caps = [
        section_header_top(section) - NEXT_ROW_HEADER_BADGE_CLEARANCE
        for section in graph.sections.values()
        if section.bbox_w > 0
        and section.bbox_h > 0
        and section.bbox_x < span_hi
        and section.bbox_x + section.bbox_w > span_lo
    ]
    channel_y = min([channel_y, *crossed_header_caps])
    return _entry_wrap_record(
        ctx,
        f.edge,
        src,
        pos_n=pos_n,
        delta=delta,
        corner_x=corner_x,
        channel_y=channel_y,
        descent_x=descent_x,
    )


def _route_left_entry_over_top(
    f: _InterFacts, geometry: _EntryWrapGeometry
) -> RoutedPath:
    """Route a same-row packed-cell bypass through the row-top corridor."""
    src, tgt, ctx = f.src, f.tgt, f.ctx
    route = _route_entry_wrap(
        f.edge,
        src,
        tgt,
        ctx,
        pos_n=geometry.pos_n,
        delta=geometry.delta,
        corner_x=geometry.corner_x,
        channel_y=geometry.channel_y,
        descent_x=geometry.descent_x,
        entry_side=PortSide.LEFT,
        normalize_exempt=False,
    )
    _declare_channel(
        route,
        ctx,
        geometry.corner_x,
        vertical_direction(geometry.channel_y - src.y),
    )
    _declare_channel(
        route,
        ctx,
        geometry.descent_x,
        vertical_direction(tgt.y - geometry.channel_y),
    )
    return route


class _MergeEntryRoute(Enum):
    """Leaf selected for a merge feeder by the dispatch table."""

    STRAIGHT = "straight"
    CORRIDOR = "corridor"
    AROUND_BELOW = "around_below"
    PERPENDICULAR_ENTRY = "perpendicular_entry"
    L_SHAPE = "l_shape"


def _merge_entry_route_kind(f: _InterFacts) -> _MergeEntryRoute:
    """Classify a non-bypass merge-junction feed without building the route.

    A near-collinear feed connects ``STRAIGHT`` to avoid a cramped curve.  A
    LEFT entry whose L-shape horizontal would cross a foreign section descends
    the clear ``CORRIDOR`` if one exists, else loops ``AROUND_BELOW``.  A
    TOP/BOTTOM entry is approached down its own column, so its feeders take the
    ``PERPENDICULAR_ENTRY`` staircase whose horizontal leg runs in the inter-row
    gap; the ``L_SHAPE`` would instead turn onto the port's own Y, which for a
    perpendicular port is the section's own top or bottom edge.
    """
    src, ctx, graph = f.src, f.ctx, f.graph
    ep = f.merge_ep
    assert ep is not None
    if abs(ep.y - f.sy) < ctx.curve_radius:
        return _MergeEntryRoute.STRAIGHT
    ep_port = graph.ports.get(ep.id)
    if ep_port and ep_port.side in (PortSide.TOP, PortSide.BOTTOM):
        return _MergeEntryRoute.PERPENDICULAR_ENTRY
    if ep_port and ep_port.side == PortSide.LEFT:
        exclude = {src.section_id} if src.section_id else set[str]()
        if f.h_segment_crosses_other_section(f.sx, ep.x, ep.y, exclude):
            if _corridor_is_viable(ctx, src, ep):
                return _MergeEntryRoute.CORRIDOR
            return _MergeEntryRoute.AROUND_BELOW
    return _MergeEntryRoute.L_SHAPE


def _route_perpendicular_entry_stair(
    edge: Edge, src: Station, entry_port: Station, n: int, ctx: _RoutingCtx
) -> RoutedPath | None:
    """Feed a merge whose entry port sits on a TOP or BOTTOM section edge.

    The same staircase the direct feeders of that port take, so a merged feeder
    and an unmerged one reach the port down one column through one inter-row
    channel rather than along two different shapes.

    The channel is pinned to the gap touching the port's own edge.  A merge
    feeder starts at a junction, which stands off the section grid and so names
    no row of its own; left to derive the channel from its source the staircase
    would traverse the gap under the feeder's origin and then run the shared
    column the whole way down, through every box between.
    """
    port = ctx.graph.ports.get(entry_port.id)
    assert port is not None and port.side in (PortSide.TOP, PortSide.BOTTOM)
    tgt_sec = resolve_section(ctx.graph, entry_port)
    if tgt_sec is None:
        return None
    if port.side is PortSide.TOP:
        channel_y = held_in_reserved_band(
            _top_entry_above_channel_y(ctx, tgt_sec),
            ctx.reserved_bands.rows.at(tgt_sec.grid_row),
        )
        return _route_top_entry_l_shape(edge, src, entry_port, n, ctx, channel_y)
    channel_y = held_in_reserved_band(
        _bottom_entry_below_channel_y(ctx, tgt_sec),
        ctx.reserved_bands.rows.at(tgt_sec.grid_row + 1),
    )
    return _route_bottom_entry_l_shape(edge, src, entry_port, n, ctx, channel_y)


def _route_merge_entry_kind(
    f: _InterFacts, kind: _MergeEntryRoute
) -> RoutedPath | None:
    """Build one already-classified merge-entry leaf."""
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
            edge, src, ep, f.i, f.n, ctx
        ),
        _MergeEntryRoute.AROUND_BELOW: lambda: _route_around_section_below(
            edge, src, tgt, ep, f.i, f.n, ctx
        ),
        _MergeEntryRoute.PERPENDICULAR_ENTRY: lambda: _route_perpendicular_entry_stair(
            edge, src, ep, f.n, ctx
        ),
        _MergeEntryRoute.L_SHAPE: lambda: _route_l_shape(edge, src, ep, f.i, f.n, ctx),
    }
    return builders[kind]()


def _route_merge_entry_family(f: _InterFacts) -> RoutedPath | None:
    """Build the ordinary L-shape merge-entry remainder."""
    return _route_merge_entry_kind(f, _MergeEntryRoute.L_SHAPE)


def _route_merge_entry_straight(f: _InterFacts) -> RoutedPath | None:
    return _route_merge_entry_kind(f, _MergeEntryRoute.STRAIGHT)


def _route_merge_entry_corridor(f: _InterFacts) -> RoutedPath | None:
    return _route_merge_entry_kind(f, _MergeEntryRoute.CORRIDOR)


def _route_merge_entry_around_below(f: _InterFacts) -> RoutedPath | None:
    return _route_merge_entry_kind(f, _MergeEntryRoute.AROUND_BELOW)


def _route_merge_entry_perpendicular(f: _InterFacts) -> RoutedPath | None:
    return _route_merge_entry_kind(f, _MergeEntryRoute.PERPENDICULAR_ENTRY)


def _takes_merge_entry_kind(f: _InterFacts, kind: _MergeEntryRoute) -> bool:
    return f.merge_ep is not None and f.merge_entry_route is kind


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
    return f.h_segment_crosses_other_section(f.sx, f.tx, f.ty)


def _route_right_entry_plough_bypass(f: _InterFacts) -> RoutedPath | None:
    # Columns are guaranteed non-None by _right_entry_plough_needs_bypass.
    assert f.src_col is not None and f.tgt_col is not None
    return _route_bypass(f, _bypass_geometry(f))


@dataclass(frozen=True)
class _Rule:
    """One row of the dispatch table: a named predicate and its route builder."""

    family_id: RouteFamilyId
    name: str
    when: Callable[[_InterFacts], bool]
    route: Callable[[_InterFacts], RoutedPath | None]


def _make_disjoint_rules(claims: Sequence[_Rule]) -> list[_Rule]:
    """Partition canonical claims into pairwise-disjoint residual predicates."""

    def make_rule(claim: _Rule) -> _Rule:
        def owns_residual(
            facts: _InterFacts,
            *,
            family_id: RouteFamilyId = claim.family_id,
        ) -> bool:
            return facts.canonical_family_id is family_id

        return _Rule(claim.family_id, claim.name, owns_residual, claim.route)

    return [make_rule(claim) for claim in claims]


_INTER_SECTION_CLAIMS: tuple[_Rule, ...] = (
    # A TOP/BOTTOM exit feeding a LEFT/RIGHT entry on the target's far side
    # wraps around the target before the generic perpendicular-exit handlers
    # can descend on its near side and cross the box to reach the port.
    _Rule(
        RouteFamilyId.PERP_EXIT_FAR_SIDE_WRAP,
        "perp-exit -> far-side entry wrap",
        lambda f: f.is_perp_exit_farside_entry_wrap,
        lambda f: _route_perp_exit_farside_entry_wrap(f),
    ),
    # A perpendicular (TOP/BOTTOM) exit leaves vertically: route it before the
    # same-Y shortcut, which would graze both boxes when exit and entry share an
    # edge Y.
    _Rule(
        RouteFamilyId.PERP_EXIT,
        "perp-exit",
        lambda f: f.is_perp_exit,
        lambda f: _route_perp_exit(f),
    ),
    # A TB/BT trailing perp exit feeding an entry against the flow (a side entry
    # at/above a downward exit, or a perpendicular entry on the target's far
    # side) cannot be reached by the flow-direction drop without grazing the
    # exit edge or clawing back through the box, so it takes the up/down-and-over
    # corridor shape before the same-Y shortcut and the TB bottom-exit drop below
    # claim it.
    _Rule(
        RouteFamilyId.TB_PERP_EXIT_OVER,
        "TB perp-exit over",
        lambda f: f.is_tb_perp_exit_against_flow,
        lambda f: _route_perp_exit_over(
            f.edge, _perp_exit_over_geometry(f.edge, f.src, f.tgt, f.ctx), f.ctx
        ),
    ),
    # Same Y, no obstacle, neither a right- nor a left-entry far-side plough: a
    # straight horizontal run.  A far-side entry (source past the port's outward
    # edge) would cut through the target interior to reach the port, so it cedes
    # to the wrap families below.
    _Rule(
        RouteFamilyId.SAME_Y_STRAIGHT,
        "same-Y straight",
        lambda f: (
            f.same_y
            and not f.needs_bypass
            and not f.right_entry_from_left
            and not f.left_entry_from_right
        ),
        _route_straight_connector,
    ),
    # A TB bottom-exit drop whose column has sections stacked between the source
    # and the (folded-below) target diverts around them through a clear gap; the
    # plain straight drop below would plough those boxes.  Checked first so only
    # the obstructed feeders divert and adjacent ones keep the straight drop.
    _Rule(
        RouteFamilyId.TB_BOTTOM_EXIT_AROUND_STACK,
        "TB bottom exit around stack",
        lambda f: f.tb_bottom_exit_drops_through_stack,
        lambda f: _route_around_stack(f),
    ),
    _Rule(
        RouteFamilyId.TB_BOTTOM_EXIT,
        "TB bottom exit",
        lambda f: f.is_tb_bottom_exit,
        lambda f: _route_tb_bottom_exit(f.edge, f.src, f.tgt, f.ctx),
    ),
    # TOP entry needs an L-shape lead-in; checked before the same-X shortcut,
    # which would drop straight in with no horizontal approach.
    _Rule(
        RouteFamilyId.TOP_ENTRY_L_SHAPE,
        "TOP entry L-shape",
        lambda f: f.entry_side is PortSide.TOP,
        lambda f: _route_top_entry_l_shape(f.edge, f.src, f.tgt, f.n, f.ctx),
    ),
    # BOTTOM entry needs the mirror-image L-shape lead-in, for the same reason.
    _Rule(
        RouteFamilyId.BOTTOM_ENTRY_L_SHAPE,
        "BOTTOM entry L-shape",
        lambda f: f.entry_side is PortSide.BOTTOM,
        lambda f: _route_bottom_entry_l_shape(f.edge, f.src, f.tgt, f.n, f.ctx),
    ),
    # Same X, but NOT a stacked LEFT-exit -> LEFT-entry (shares the column's
    # left-edge X: a straight drop would run down the source box; the serpentine
    # rule below leads it out into a clear left-of-column channel) nor a stacked
    # RIGHT-exit -> RIGHT-entry (shares the right-edge X: the RIGHT-entry wrap
    # bows it out past the port's outward edge so both ports curve and a
    # co-terminating feed shares the descent channel).
    _Rule(
        RouteFamilyId.SAME_X_VERTICAL_DROP,
        "same-X vertical drop",
        lambda f: (
            f.same_x
            and not f.is_serpentine_left_exit_left_entry
            and not f.is_stacked_right_exit_right_entry
        ),
        _route_straight_connector,
    ),
    _Rule(
        RouteFamilyId.BOTTOM_EXIT_JUNCTION_RIGHT_LANDINGS,
        "bottom-exit junction right landings",
        lambda f: (
            f.edge.source in f.ctx.bottom_exit_junctions
            and f.bottom_exit_junction_route is _BottomExitJunctionRoute.FAN_LANDINGS
        ),
        lambda f: _route_bottom_exit_junction_right_landings(f),
    ),
    _Rule(
        RouteFamilyId.BOTTOM_EXIT_JUNCTION_VIA_GAP,
        "bottom-exit junction via gap",
        lambda f: (
            f.edge.source in f.ctx.bottom_exit_junctions
            and f.bottom_exit_junction_route is _BottomExitJunctionRoute.VIA_GAP
        ),
        lambda f: _route_bottom_exit_junction_via_gap_leaf(f),
    ),
    _Rule(
        RouteFamilyId.BOTTOM_EXIT_JUNCTION,
        "bottom-exit junction",
        lambda f: (
            f.edge.source in f.ctx.bottom_exit_junctions
            and f.bottom_exit_junction_route is _BottomExitJunctionRoute.PLAIN
        ),
        lambda f: _route_bottom_exit_junction(f),
    ),
    # Every feeder of a merge that has a trunk routes through the merge
    # handlers so the converging line is a single stroke: the trunk carries the
    # full bypass to the entry port, every other feeder descends onto the
    # trunk's channel.  These precede the bypass / merge-entry rules, which
    # would otherwise route a non-bypass feeder straight into the entry on its
    # own lateral slot (a second parallel stroke).
    _Rule(
        RouteFamilyId.MERGE_TRUNK_AROUND_BELOW,
        "merge trunk around below",
        lambda f: f.is_merge_trunk and f.merge_trunk_shape.around_below,
        lambda f: _route_merge_trunk_around_below(f),
    ),
    _Rule(
        RouteFamilyId.MERGE_TRUNK,
        "merge trunk",
        lambda f: f.is_merge_trunk and not f.merge_trunk_shape.around_below,
        lambda f: _route_merge_trunk(f, f.merge_trunk_shape),
    ),
    _Rule(
        RouteFamilyId.MERGE_BRANCH,
        "merge branch",
        lambda f: f.is_merge_branch,
        _route_merge_branch_feeder,
    ),
    _Rule(
        RouteFamilyId.LEFT_ENTRY_CORRIDOR,
        "LEFT entry corridor",
        _takes_left_entry_corridor,
        _route_left_entry_corridor,
    ),
    _Rule(
        RouteFamilyId.BYPASS_L_SHAPE,
        "bypass L-shape",
        lambda f: f.needs_bypass and f.bypass_route is _BypassRoute.L_SHAPE,
        lambda f: _route_l_shape(f.edge, f.src, f.tgt, f.i, f.n, f.ctx),
    ),
    _Rule(
        RouteFamilyId.BYPASS_CELLMATE_GAP_DROP,
        "bypass cell-mate gap drop",
        lambda f: f.needs_bypass and f.bypass_route is _BypassRoute.CELLMATE_GAP_DROP,
        _route_bypass_cellmate_gap_drop,
    ),
    _Rule(
        RouteFamilyId.BYPASS_PACKED_CELL_SAME_ROW,
        "bypass packed-cell same row",
        lambda f: (
            f.needs_bypass and f.bypass_route is _BypassRoute.PACKED_CELL_SAME_ROW
        ),
        _route_bypass_packed_cell_same_row,
    ),
    _Rule(
        RouteFamilyId.BYPASS_RIGHT_ENTRY_CROSS_ROW,
        "bypass RIGHT entry cross-row",
        lambda f: (
            f.needs_bypass and f.bypass_route is _BypassRoute.RIGHT_ENTRY_CROSS_ROW
        ),
        _route_right_entry_cross_row,
    ),
    _Rule(
        RouteFamilyId.BYPASS_LEFT_ENTRY,
        "bypass LEFT entry",
        lambda f: f.needs_bypass and f.bypass_route is _BypassRoute.LEFT_ENTRY_FAMILY,
        _route_left_entry_family,
    ),
    _Rule(
        RouteFamilyId.BYPASS_LEFT_EXIT_AROUND_BELOW,
        "bypass LEFT exit around below",
        lambda f: (
            f.needs_bypass and f.bypass_route is _BypassRoute.LEFT_EXIT_AROUND_BELOW
        ),
        _route_left_exit_around_below_left_entry,
    ),
    _Rule(
        RouteFamilyId.BYPASS_FAMILY,
        "bypass family",
        lambda f: f.needs_bypass and f.bypass_route is _BypassRoute.U_BYPASS,
        _route_u_bypass_family,
    ),
    _Rule(
        RouteFamilyId.NEAR_VERTICAL_JUNCTION,
        "near-vertical same-col junction",
        lambda f: f.takes_near_vertical_junction_drop,
        _route_near_vertical_junction,
    ),
    # RIGHT entry fed from the left: wrap around the right side (over the top for
    # a same-row source, below the source row for a cross-row one) rather than
    # cut through the interior.
    _Rule(
        RouteFamilyId.RIGHT_ENTRY_WRAP,
        "RIGHT entry wrap",
        lambda f: (
            (f.entry_side is PortSide.RIGHT and f.horizontal is Direction.R)
            or f.is_stacked_right_exit_right_entry
        ),
        lambda f: _route_right_entry_wrap(f),
    ),
    _Rule(
        RouteFamilyId.LEFT_ENTRY_WRAP,
        "LEFT entry wrap family",
        lambda f: (
            f.entry_side is PortSide.LEFT
            and f.dx < 0
            and f.cross_row
            and not f.is_serpentine_left_exit_left_entry
        ),
        _route_left_entry_family,
    ),
    _Rule(
        RouteFamilyId.SERPENTINE_LEFT,
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
        RouteFamilyId.LEFT_EXIT_FAR_SIDE_WRAP,
        "LEFT exit -> far-side LEFT entry wrap",
        lambda f: f.left_entry_from_right and f.is_left_exit,
        _route_left_exit_around_below_left_entry,
    ),
    _Rule(
        RouteFamilyId.MERGE_ENTRY_STRAIGHT,
        "merge entry straight",
        lambda f: _takes_merge_entry_kind(f, _MergeEntryRoute.STRAIGHT),
        _route_merge_entry_straight,
    ),
    _Rule(
        RouteFamilyId.MERGE_ENTRY_CORRIDOR,
        "merge entry corridor",
        lambda f: _takes_merge_entry_kind(f, _MergeEntryRoute.CORRIDOR),
        _route_merge_entry_corridor,
    ),
    _Rule(
        RouteFamilyId.MERGE_ENTRY_AROUND_BELOW,
        "merge entry around below",
        lambda f: _takes_merge_entry_kind(f, _MergeEntryRoute.AROUND_BELOW),
        _route_merge_entry_around_below,
    ),
    _Rule(
        RouteFamilyId.MERGE_ENTRY_PERPENDICULAR,
        "merge entry perpendicular",
        lambda f: _takes_merge_entry_kind(f, _MergeEntryRoute.PERPENDICULAR_ENTRY),
        _route_merge_entry_perpendicular,
    ),
    _Rule(
        RouteFamilyId.MERGE_ENTRY,
        "merge entry family",
        lambda f: _takes_merge_entry_kind(f, _MergeEntryRoute.L_SHAPE),
        _route_merge_entry_family,
    ),
    # A higher-row L-shape to a RIGHT entry that would plough an intervening
    # same-row section deflects through the bypass instead.
    _Rule(
        RouteFamilyId.RIGHT_ENTRY_PLOUGH_BYPASS,
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
        RouteFamilyId.RIGHT_ENTRY_CROSS_ROW_WRAP,
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
)

_INTER_SECTION_RULES: list[_Rule] = _make_disjoint_rules(_INTER_SECTION_CLAIMS)

_INDEXED_INTER_SECTION_RULES = _INTER_SECTION_RULES
_INTER_SECTION_RULE_BY_FAMILY = MappingProxyType(
    {rule.family_id: rule for rule in _INDEXED_INTER_SECTION_RULES}
)

CLASSIFIABLE_INTER_SECTION_FAMILIES = frozenset(_INTER_SECTION_RULE_BY_FAMILY) | {
    RouteFamilyId.STANDARD_L_SHAPE
}
"""Every family :func:`classify_inter_section_family` can name before emission.

A rule's own family, or the standard L-shape the classifier falls to when no
rule claims the edge. Local fallback handlers and rail mode fix their families
outside the inter-section classifier, so neither is a family this classifier
can return.
"""
if len(_INTER_SECTION_RULE_BY_FAMILY) != len(_INDEXED_INTER_SECTION_RULES):
    raise RuntimeError("inter-section route families are not unique")


def _inter_section_rule_for_family(family_id: RouteFamilyId) -> _Rule | None:
    """Resolve a frozen family without re-running dispatch predicates."""
    if _INTER_SECTION_RULES is _INDEXED_INTER_SECTION_RULES:
        return _INTER_SECTION_RULE_BY_FAMILY.get(family_id)
    return next(
        (rule for rule in _INTER_SECTION_RULES if rule.family_id is family_id),
        None,
    )


def _route_inter_section(
    edge: Edge,
    src: Station,
    tgt: Station,
    ctx: _RoutingCtx,
    *,
    planned_family_id: RouteFamilyId,
    observer: RoutePlanObserver | None = None,
) -> RoutedPath | None:
    """Build an inter-section edge from its frozen family.

    Returns ``None`` when the edge is not inter-section (both endpoints must be
    a port or junction) or when the frozen family declines its member.
    """
    is_inter = (src.is_port or edge.source in ctx.junction_ids) and (
        tgt.is_port or edge.target in ctx.junction_ids
    )
    if not is_inter:
        return None

    f = _build_inter_facts(edge, src, tgt, ctx)
    rule = _inter_section_rule_for_family(planned_family_id)
    if rule is not None:
        family_id = rule.family_id
        route = rule.route(f)
    else:
        # Standard L-shape: the default when no rule above claims the edge.
        family_id = planned_family_id
        if family_id is not RouteFamilyId.STANDARD_L_SHAPE:
            raise RuntimeError(f"planned route family {family_id.value!r} is unknown")
        route = _route_l_shape(edge, src, tgt, f.i, f.n, ctx)
    from nf_metro.layout.route_plan import ExitTurnDisposition
    from nf_metro.layout.routing.exit_turns import (
        ExitTurnInvariantError,
        consume_exit_turn_route,
        exit_turn_failure,
    )

    membership = (
        ctx.exit_turns.membership_for_edge(edge) if ctx.exit_turns is not None else None
    )
    if (
        route is None
        and membership is not None
        and membership.assignment is not None
        and membership.plan.disposition is ExitTurnDisposition.PLANNED
    ):
        raise ExitTurnInvariantError(
            exit_turn_failure(
                membership.plan,
                f"member {membership.member_id} declined its {family_id.value} emitter",
            )
        )
    if route is not None:
        consume_exit_turn_route(route, family_id, ctx)
        from nf_metro.layout.routing.convergences import consume_convergence_route

        consume_convergence_route(route, ctx)
    if observer is not None and route is not None:
        observer.record_dispatch((edge.source, edge.target, edge.line_id), family_id)
    _declare_trunk(route, ctx)
    return route


def classify_inter_section_family(
    edge: Edge,
    src: Station,
    tgt: Station,
    ctx: _RoutingCtx,
) -> RouteFamilyId | None:
    """Return the production family selected before its builder is invoked."""
    is_inter = (src.is_port or edge.source in ctx.junction_ids) and (
        tgt.is_port or edge.target in ctx.junction_ids
    )
    if not is_inter:
        return None
    facts = _build_inter_facts(edge, src, tgt, ctx)
    rule = _match_inter_section_rule(facts)
    return rule.family_id if rule is not None else RouteFamilyId.STANDARD_L_SHAPE


def _match_inter_section_rule(f: _InterFacts) -> _Rule | None:
    """The dispatch rule whose predicate claims *f*, or ``None``.

    The selection seam: ``_route_inter_section`` routes through the matched
    rule, and the dispatch-table tests assert which rule claims each edge so a
    predicate edit that silently steals an edge class from a neighbouring rule
    is caught.
    """
    for rule in _INTER_SECTION_RULES:
        if rule.when(f):
            return rule
    return None


@dataclass(frozen=True, slots=True)
class _TbBottomExitGeometry:
    points: tuple[tuple[float, float], ...]
    lane_offset: float
    bundle_offsets: tuple[float, ...] | None
    seam: _SourceSeam


def _tb_bottom_exit_geometry(
    edge: Edge, src: Station, tgt: Station, ctx: _RoutingCtx
) -> _TbBottomExitGeometry:
    """Resolve the source seam shared by TB bottom-exit planning and emission."""
    run_direction = vertical_direction(tgt.y - src.y)
    if needs_perp_approach_fan(ctx.graph, edge.target):
        land_x = _perp_approach_fan_x(ctx, edge.target, edge.line_id, tgt.x)
        channel_y = inter_row_channel_y(
            ctx.graph,
            src,
            tgt,
            src.y,
            tgt.y,
            tgt.y - src.y,
            ctx.curve_radius,
            reserved=ctx.reserved_bands.rows,
        )
        lo, hi = sorted((src.y, tgt.y))
        channel_y = min(max(channel_y, lo + ctx.curve_radius), hi - ctx.curve_radius)
        turn_direction = horizontal_direction(land_x - src.x)
        return _TbBottomExitGeometry(
            ((src.x, src.y), (src.x, channel_y), (land_x, channel_y), (land_x, tgt.y)),
            0.0,
            None,
            _SourceSeam(run_direction, turn_direction, src.y, channel_y),
        )

    x_offset = _tb_x_offset(ctx, edge.source, edge.line_id, src.section_id)
    source_x = src.x + x_offset
    target_x = tgt.x + x_offset
    if abs(target_x - source_x) <= COORD_TOLERANCE:
        return _TbBottomExitGeometry(
            ((source_x, src.y), (target_x, tgt.y)),
            0.0,
            None,
            _SourceSeam(run_direction, None, None, None),
        )

    channel_y = inter_row_channel_y(
        ctx.graph,
        src,
        tgt,
        src.y,
        tgt.y,
        tgt.y - src.y,
        ctx.curve_radius,
        reserved=ctx.reserved_bands.rows,
    )
    _members, line_ids, _edge_by_line = gather_member_edges(ctx.graph, edge)
    riser_sign = -run_direction.sign

    def lane_offset(line_id: str) -> float:
        return riser_sign * _tb_x_offset(ctx, edge.source, line_id, src.section_id)

    fan_clearance = INTER_ROW_EDGE_CLEARANCE + (len(line_ids) - 1) * ctx.offset_step
    channel_y = src.y + run_direction.sign * max(
        (channel_y - src.y) * run_direction.sign,
        fan_clearance,
    )
    lo, hi = sorted((src.y, tgt.y))
    channel_y = min(max(channel_y, lo + ctx.curve_radius), hi - ctx.curve_radius)
    turn_direction = horizontal_direction(tgt.x - src.x)
    own_offset = lane_offset(edge.line_id)
    axis_coordinate = channel_y + own_offset * turn_direction.sign
    return _TbBottomExitGeometry(
        (
            (src.x, src.y),
            (src.x, channel_y),
            (tgt.x, channel_y),
            (tgt.x, tgt.y),
        ),
        own_offset,
        tuple(lane_offset(line_id) for line_id in line_ids),
        _SourceSeam(run_direction, turn_direction, src.y, axis_coordinate),
    )


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
    planned_transition = _route_planned_lane_transition(
        edge,
        ctx,
        is_inter_section=True,
    )
    if planned_transition is not None:
        return planned_transition
    geometry = _tb_bottom_exit_geometry(edge, src, tgt, ctx)
    return route_along(
        edge,
        [(edge, edge.line_id, geometry.lane_offset)],
        list(geometry.points),
        base_radius=ctx.curve_radius,
        bundle_offsets=list(geometry.bundle_offsets)
        if geometry.bundle_offsets is not None
        else None,
        normalize_exempt=False if geometry.seam.turn_direction is None else True,
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


@dataclass(frozen=True, slots=True)
class _AroundStackGeometry:
    points: tuple[tuple[float, float], ...]
    lane_offset: float
    bundle_offsets: tuple[float, ...]
    channel_x: float
    channel_y_lo: float
    channel_y_hi: float
    seam: _SourceSeam
    cross_lo: float
    cross_hi: float


def _around_stack_geometry(
    f: _InterFacts,
) -> _AroundStackGeometry:
    """Resolve the stack-bypass channel shared by planning and emission.

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
    # seat the corridor a fan width below the clearance that innermost lane owes
    # the bottom edge.  That clearance is the one the row-gap reservation is
    # measured against, and a planned turn axis is frozen against the settlement
    # that would otherwise push the ladder onto it, so it is stated here.
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
        src_bottom + fan_clearance,
    )
    cy_entry = header_corridor_y(
        graph, tgt_sec.grid_row, below=False, base_radius=ctx.curve_radius, default=ty
    )
    vx = _around_stack_channel_x(f)

    own_offset = lane_offset(edge.line_id)
    channel_y_start = cy_down - own_offset
    channel_y_end = cy_entry + own_offset
    points = (
        (sx, sy),
        (sx, cy_down),
        (vx, cy_down),
        (vx, cy_entry),
        (tx, cy_entry),
        (tx, ty),
    )
    # Both legs the jog joins descend, so the jog's ends and the jog itself take
    # their shift from the same right-hand normal (``bundle._right_normal``): one
    # lateral off the exit X, off the channel X, and off the corridor Y.
    channel_x = vx - own_offset
    run_direction = segment_direction(points[0], points[1])
    turn_direction = segment_direction(points[1], points[2])
    assert run_direction is not None and turn_direction is not None
    return _AroundStackGeometry(
        points,
        own_offset,
        tuple(lane_offset(line_id) for line_id in line_ids),
        channel_x,
        min(channel_y_start, channel_y_end),
        max(channel_y_start, channel_y_end),
        _SourceSeam(
            run_direction,
            turn_direction,
            sy,
            cy_down + own_offset * turn_direction.sign,
        ),
        min(sx - own_offset, channel_x),
        max(sx - own_offset, channel_x),
    )


def _route_around_stack(f: _InterFacts) -> RoutedPath | None:
    """Route a TB bottom-exit feeder around sections stacked below it."""
    geometry = _around_stack_geometry(f)
    edge, ctx = f.edge, f.ctx

    route = route_along(
        edge,
        [(edge, edge.line_id, geometry.lane_offset)],
        list(geometry.points),
        base_radius=ctx.curve_radius,
        bundle_offsets=list(geometry.bundle_offsets),
    )
    _declare_channel(route, ctx, geometry.points[2][0], Direction.D)
    return route


@dataclass(frozen=True, slots=True)
class _BottomExitJunctionGeometry:
    """The plain vertical-drop-then-turn seam shared by planning and emission.

    Only describes the shape ``_route_bottom_exit_junction`` draws when
    neither the right-landings fan plan nor the inter-section-crossing detour
    claims the edge -- both draw a different shape this record does not
    state.
    """

    vx: float
    hy: float
    lane_offset: float
    seam: _SourceSeam


def _bottom_exit_junction_exit_port(
    ctx: _RoutingCtx, source_id: str
) -> tuple[str, str]:
    """The (exit port id, its section id) a bottom-exit junction descends from."""
    exit_pid = ctx.bottom_exit_junction_ports[source_id]
    exit_station = ctx.graph.stations.get(exit_pid)
    exit_sec = exit_station.section_id if exit_station else None
    return exit_pid, exit_sec or ""


def _bottom_exit_junction_geometry(
    edge: Edge,
    src: Station,
    tgt: Station,
    exit_x_offset: Callable[[str], float],
    members: list[_TaperedMember],
    tgt_center: float,
) -> _BottomExitJunctionGeometry:
    """Resolve the plain bottom-exit-junction seam for *edge*.

    ``lane_offset`` is the rigid perpendicular offset *edge* keeps on both
    legs (its own displacement from the bundle's exit mean); projecting it
    through the turn onto the horizontal leg is what makes ``axis_coordinate``
    the row this line actually turns on.
    """
    exit_offs = [exit_x_offset(line_id) for _e, line_id, _s, _t in members]
    vx = src.x + sum(exit_offs) / len(exit_offs)
    hy = tgt.y + tgt_center
    lane_offset = next(s for _e, lid, s, _t in members if lid == edge.line_id)
    turn_direction = segment_direction((vx, hy), (tgt.x, hy))
    run_direction = segment_direction((vx, src.y), (vx, hy))
    assert turn_direction is not None and run_direction is not None
    return _BottomExitJunctionGeometry(
        vx,
        hy,
        lane_offset,
        _SourceSeam(
            run_direction, turn_direction, src.y, hy + lane_offset * turn_direction.sign
        ),
    )


def _bottom_exit_junction_is_right_landings(edge: Edge, ctx: _RoutingCtx) -> bool:
    """Whether a fan plan's right-landings emitter, not the plain L, draws *edge*."""
    query = ctx.graph.fan_plan_query
    if query is None:
        return False
    binding = query.route_emission_for_resolved_edge(
        ResolvedEdge(edge.source, edge.target, edge.line_id)
    )
    return (
        binding is not None
        and binding[0].disposition is FanPlanDisposition.PLANNED
        and binding[2].emitter is FanRouteEmitter.BOTTOM_EXIT_RIGHT_LANDINGS
    )


def _bottom_exit_junction_parts(
    f: _InterFacts,
) -> tuple[
    _BottomExitJunctionGeometry,
    list[_TaperedMember],
    list[_TaperedMember],
    Callable[[str], float],
]:
    """Resolve the geometry and members shared by all junction leaves."""
    edge, src, tgt, ctx = f.edge, f.src, f.tgt, f.ctx
    exit_pid, exit_sec = _bottom_exit_junction_exit_port(ctx, edge.source)

    def exit_x_offset(line_id: str) -> float:
        if ctx.station_offsets:
            return _tb_x_offset(ctx, exit_pid, line_id, exit_sec)
        bi, bn = ctx.bundle_info.get((edge.source, edge.target, line_id), (f.i, f.n))
        return l_shape_stagger(bi, bn, Direction.D, ctx.offset_step)

    members, _, tgt_center = gather_tapered_bundle(ctx, edge)
    geometry = _bottom_exit_junction_geometry(
        edge, src, tgt, exit_x_offset, members, tgt_center
    )
    rigid = [(e, line_id, src_off, src_off) for e, line_id, src_off, _tgt in members]
    return geometry, members, rigid, exit_x_offset


def _bottom_exit_junction_route_kind(f: _InterFacts) -> _BottomExitJunctionRoute:
    """Name the bottom-exit-junction geometry production will emit."""
    if _bottom_exit_junction_is_right_landings(f.edge, f.ctx):
        return _BottomExitJunctionRoute.FAN_LANDINGS
    geometry, _members, rigid, _offset = _bottom_exit_junction_parts(f)
    if (
        f.h_segment_crosses_other_section(geometry.vx, f.tgt.x, geometry.hy)
        and _route_bottom_exit_junction_via_gap(
            f.edge, f.src, f.tgt, f.ctx, geometry.vx, rigid
        )
        is not None
    ):
        return _BottomExitJunctionRoute.VIA_GAP
    return _BottomExitJunctionRoute.PLAIN


def _route_bottom_exit_junction_leaf(
    f: _InterFacts, kind: _BottomExitJunctionRoute
) -> RoutedPath | None:
    """Build one already-classified bottom-exit-junction leaf.

    The descent channel sits at the bundle's mean exit X (the fan above the
    junction), turns the corner, and runs to the entry at the mean entry Y.
    Because the channel is anchored on the exit fan rather than the per-line
    endpoint offsets, this corner is fanned rigidly -- one offset on every leg
    -- so the bundle is built with each line's source offset on both ends and
    ``route_tapered`` sends it down its rigid (``route_along``) path.
    """
    edge, src, tgt, ctx = f.edge, f.src, f.tgt, f.ctx
    geometry, members, rigid, exit_x_offset = _bottom_exit_junction_parts(f)
    vx, hy = geometry.vx, geometry.hy
    if kind is _BottomExitJunctionRoute.FAN_LANDINGS:
        return _route_planned_bottom_exit_right_landings(
            edge, src, tgt, ctx, members, exit_x_offset, hy
        )
    if kind is _BottomExitJunctionRoute.VIA_GAP:
        return _route_bottom_exit_junction_via_gap(edge, src, tgt, ctx, vx, rigid)
    return route_tapered(
        edge,
        rigid,
        [(vx, src.y), (vx, hy), (tgt.x, hy)],
        transition_leg=1,
        base_radius=ctx.curve_radius,
    )


def _route_bottom_exit_junction(f: _InterFacts) -> RoutedPath | None:
    return _route_bottom_exit_junction_leaf(f, _BottomExitJunctionRoute.PLAIN)


def _route_bottom_exit_junction_right_landings(f: _InterFacts) -> RoutedPath | None:
    return _route_bottom_exit_junction_leaf(f, _BottomExitJunctionRoute.FAN_LANDINGS)


def _route_bottom_exit_junction_via_gap_leaf(f: _InterFacts) -> RoutedPath | None:
    return _route_bottom_exit_junction_leaf(f, _BottomExitJunctionRoute.VIA_GAP)


def _planned_fan_launch_y(
    ctx: _RoutingCtx,
    edge: Edge,
    fork_y: float,
    runway: float,
    source_offsets: Mapping[str, float],
    source_lines: Sequence[str],
) -> float:
    """The traverse height the fan's own row-gap reservation allocates it.

    The traverse's lanes are drawn at this height plus their own source offsets,
    so the band has to hold the whole spread: the leading lane seats it and the
    trailing one holds it.  The runway floors the result, and the reservation
    states that floor as its own negative-side edge, so the two agree wherever
    the boundary is settled and the runway wins wherever it is not.  The first
    routing pass publishes the ledger and so reads no band, which leaves the
    floor alone to place the traverse.
    """
    floor = fork_y + runway
    band = ctx.reserved_bands.claimed_row_band(edge.source, edge.target, edge.line_id)
    if band is None:
        return floor
    lanes = [source_offsets[line_id] for line_id in source_lines]
    return max(floor, min(band.lo - min(lanes), band.hi - max(lanes)))


def _route_planned_bottom_exit_right_landings(
    edge: Edge,
    src: Station,
    tgt: Station,
    ctx: _RoutingCtx,
    members: list[_TaperedMember],
    exit_x_offset: Callable[[str], float],
    target_y: float,
) -> RoutedPath | None:
    query = ctx.graph.fan_plan_query
    if query is None:
        return None
    resolved = ResolvedEdge(edge.source, edge.target, edge.line_id)
    binding = query.route_emission_for_resolved_edge(resolved)
    if binding is None:
        return None
    plan, _branch, emission = binding
    target_port = ctx.graph.ports.get(edge.target)
    if (
        plan.disposition is not FanPlanDisposition.PLANNED
        or emission.emitter is not FanRouteEmitter.BOTTOM_EXIT_RIGHT_LANDINGS
        or plan.fork_station_id != edge.source
        or target_port is None
        or target_port.side is not PortSide.RIGHT
    ):
        raise RuntimeError(f"planned fan {plan.id!s} route-emission contract drifted")

    landing_sections = []
    for planned_branch in plan.branches:
        for port_id in planned_branch.landing_port_ids:
            port = ctx.graph.ports.get(port_id)
            section = (
                ctx.graph.sections.get(port.section_id) if port is not None else None
            )
            if port is None or port.side is not PortSide.RIGHT or section is None:
                raise RuntimeError(
                    f"planned fan {plan.id!s} lost a RIGHT landing section"
                )
            if section not in landing_sections:
                landing_sections.append(section)
    if len(landing_sections) != len(plan.branches):
        raise RuntimeError(f"planned fan {plan.id!s} landing ownership drifted")

    if plan.entry_runway is None:
        raise RuntimeError(f"planned fan {plan.id!s} lost its routing runway")
    right_edge = max(section.bbox_x + section.bbox_w for section in landing_sections)
    source_lines = plan.offset_line_order
    if not source_lines or edge.line_id not in source_lines:
        raise RuntimeError(f"planned fan {plan.id!s} lost its source lane order")
    source_offsets = {line_id: -exit_x_offset(line_id) for line_id in source_lines}
    corridor_x = max(
        _right_entry_descent_x(ctx, right_edge, len(source_lines)),
        right_edge + SECTION_ROUTE_CLEARANCE + max(source_offsets.values()),
    )
    launch_y = _planned_fan_launch_y(
        ctx, edge, src.y, plan.entry_runway, source_offsets, source_lines
    )
    target_offsets = {
        line_id: target_offset
        for _member_edge, line_id, _source_offset, target_offset in members
    }

    route = route_vhvh_offset(
        edge,
        members,
        source=(src.x, src.y),
        launch_y=launch_y,
        corridor_x=corridor_x,
        target=(tgt.x, target_y),
        source_offsets=source_offsets,
        target_offsets=target_offsets,
        line_order=source_lines,
        base_radius=ctx.curve_radius,
    )
    if route is None:
        raise RuntimeError(f"planned fan {plan.id!s} emitter omitted {resolved!r}")
    route.fan_plan_id = plan.id
    route.fan_route_emitter = emission.emitter.value
    _declare_placed_channels(
        route,
        ctx,
        source_lines.index(edge.line_id),
        len(source_lines),
    )
    return route


def _route_bottom_exit_junction_via_gap(
    edge: Edge,
    src: Station,
    tgt: Station,
    ctx: _RoutingCtx,
    vx: float,
    rigid: list[_TaperedMember],
) -> RoutedPath | None:
    """Detour a bottom-exit-junction feed whose horizontal leg crosses a box.

    The junction already sits in the inter-row gap below its source section, so
    the bundle drops straight down its exit channel to the header-clear gap Y,
    traverses that gap to the clear inter-column channel just left of the target
    column (above the crossed box, whose row starts below the gap), then drops
    into the target's LEFT entry::

        (vx, jy) -> (vx, gy) -> (corner_x, gy) -> (corner_x, ty) -> (tx, ty)

    Returns ``None`` for a target this shape does not cover (a non-LEFT entry,
    or one with no clear inter-column channel to its left), so the caller falls
    back to the plain L.
    """
    tgt_port = ctx.graph.ports.get(edge.target)
    if tgt_port is None or tgt_port.side != PortSide.LEFT:
        return None
    ep_col, ep_row = _resolve_section_colrow(ctx.graph, tgt)
    if ep_col is None or ep_row is None:
        return None
    corner_x = _corridor_descent_x(ctx, ep_col, ep_row, 0.0)
    if corner_x is None:
        return None
    gy = inter_row_channel_y(
        ctx.graph,
        src,
        tgt,
        src.y,
        tgt.y,
        tgt.y - src.y,
        ctx.curve_radius,
        reserved=ctx.reserved_bands.rows,
    )
    route = route_tapered(
        edge,
        rigid,
        [(vx, src.y), (vx, gy), (corner_x, gy), (corner_x, tgt.y), (tgt.x, tgt.y)],
        transition_leg=1,
        base_radius=ctx.curve_radius,
    )
    _declare_channel(route, ctx, corner_x, vertical_direction(tgt.y - gy))
    return route


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

    lead_x = _merge_branch_lead_x(src, ctx, src_col)

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


def _merge_branch_lead_x(src: Station, ctx: _RoutingCtx, src_col: int) -> float:
    """Return the source channel fixed by a merge feeder's section edge."""
    left_edge = col_left_edge(ctx.graph, src_col)
    right_edge = col_right_edge(ctx.graph, src_col)
    if src.x >= (left_edge + right_edge) / 2:
        return max(right_edge + MERGE_ROUTE_MARGIN, src.x + ctx.curve_radius)
    return min(left_edge - MERGE_ROUTE_MARGIN, src.x - ctx.curve_radius)


def _would_route_around_section_below(edge: Edge, ctx: _RoutingCtx) -> bool:
    """Whether *edge* dispatches to :func:`_route_around_section_below`.

    A merge-junction feeder reaches the around-below loop only through the
    merge-entry family, and only when :func:`_merge_entry_route_kind` selects
    it, so this consults the dispatch table rather than re-deriving the
    bypass / section-crossing predicates.
    """
    src, tgt = ctx.graph.edge_endpoints(edge)
    f = _build_inter_facts(edge, src, tgt, ctx)
    rule = _match_inter_section_rule(f)
    return rule is not None and rule.family_id is RouteFamilyId.MERGE_ENTRY_AROUND_BELOW


def _has_around_section_sibling(
    edge: Edge, ep_port: Port | None, ctx: _RoutingCtx
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


class _MergeTrunkShape(NamedTuple):
    """How a merge trunk reaches the entry port standing behind its junction.

    ``around_below`` marks the loop under the target; every other field is a
    :func:`_bypass_geometry` input for the U-shape drawn otherwise.  Stated
    apart from the builder so the plan naming the trunk's turn and the emission
    drawing it read one description of the shape.
    """

    around_below: bool
    entry_port: Station | None
    effective_tx: float
    effective_ty: float
    force_cross_row: bool
    trunk_v_up_pull_away: bool
    around_below_channel_y: float | None


def _merge_trunk_shape(f: _InterFacts) -> _MergeTrunkShape:
    """The shape the trunk carrier draws from its source to the entry port.

    Both X and Y of the entry port override the target coordinates because the
    merge junction is virtual and lives inside the target section at a
    different Y from the actual entry port; without the Y override the bypass
    terminates at the merge junction's Y and leaves a visible "hanging" curve
    disconnected from the entry port.

    A LEFT entry port with no clear inter-column channel to its left (the
    target sits in the leftmost column, fed from its right) has no gap for the
    bypass to rise in on the port's own side; the U-shape's gap2 lands to the
    RIGHT of the box and its final port-approach leg ploughs leftward through
    the target interior.  Such a trunk goes around below the target instead,
    rising on the far (left) side and entering the LEFT port from outside.  The
    around-below traverse runs at the trunk's ``bypass_bottom_y`` channel, the
    same Y the branch feeders drop onto, so the converging lines overlay as one
    stroke.

    When the trunk and entry are in the same grid row but separated by
    intervening row-mates, the standard above-row bypass channel sits in the
    inter-row gap that also holds the target row's section titles.
    ``force_cross_row`` runs the channel BELOW all sections in the column
    range instead, mirroring :func:`_route_around_section_below` and avoiding
    overlap with the title text.

    When a sibling edge to the same merge junction will route via
    :func:`_route_around_section_below`, both routes would place their V_up
    channels in the inter-column gap just left of the target section,
    producing overlapping bundles in the same x range.
    ``trunk_v_up_pull_away`` pulls the trunk's V_up channel further from the
    target edge (towards the previous column) so the two bundles occupy
    distinct columns within the gap.
    """
    edge, tgt, ctx = f.edge, f.tgt, f.ctx
    assert f.src_col is not None and f.tgt_col is not None
    ep_id = ctx.merge.entry_port_for.get(edge.target)
    ep = ctx.graph.stations.get(ep_id) if ep_id else None
    ep_port = ctx.graph.ports.get(ep_id) if ep_id else None
    effective_tx = ep.x if ep else tgt.x
    effective_ty = ep.y if ep else tgt.y

    if ep is not None and ep_port is not None and ep_port.side == PortSide.LEFT:
        ep_col, ep_row = f.section_colrow(ep)
        no_left_channel = (
            ep_col is None
            or ep_row is None
            or _corridor_descent_x(ctx, ep_col, ep_row, 0.0) is None
        )
        if no_left_channel:
            return _MergeTrunkShape(
                True,
                ep,
                effective_tx,
                effective_ty,
                False,
                False,
                ctx.merge.trunk_by.get(edge.target),
            )
    return _MergeTrunkShape(
        False,
        ep,
        effective_tx,
        effective_ty,
        merge_trunk_force_cross_row(
            ctx.graph,
            f.src_col,
            f.tgt_col,
            f.src_row,
            f.tgt_row,
        ),
        ep is not None and _has_around_section_sibling(edge, ep_port, ctx),
        None,
    )


def _route_merge_trunk(
    f: _InterFacts, shape: _MergeTrunkShape | None = None
) -> RoutedPath:
    """Full U-shape bypass for the trunk carrier, ending at the entry port."""
    shape = shape or f.merge_trunk_shape
    assert not shape.around_below
    return _route_bypass(f, _bypass_geometry(f, shape))


def _merge_trunk_around_below_geometry(
    f: _InterFacts, shape: _MergeTrunkShape
) -> _EntryWrapGeometry:
    """Resolve the around-below seam shared by trunk planning and emission."""
    assert shape.around_below and shape.entry_port is not None
    geometry = _around_section_below_geometry(
        f.ctx,
        f.edge,
        f.src,
        shape.entry_port,
        f.i,
        f.n,
        shape.around_below_channel_y,
    )
    sibling_flanks: list[float] = []
    for sibling in f.ctx.graph.edges_from(f.edge.source):
        if sibling.target == f.edge.target or sibling.line_id != f.edge.line_id:
            continue
        trunk_source = f.ctx.merge.trunk_source.get(sibling.target)
        trunk_edge = f.ctx.edge_by_key.get(
            (trunk_source or "", sibling.target, sibling.line_id)
        )
        if trunk_edge is None:
            continue
        trunk_src, trunk_tgt = f.ctx.graph.edge_endpoints(trunk_edge)
        trunk_facts = _build_inter_facts(trunk_edge, trunk_src, trunk_tgt, f.ctx)
        trunk_shape = trunk_facts.merge_trunk_shape
        if trunk_shape.around_below:
            continue
        trunk_route = _route_merge_trunk(trunk_facts, trunk_shape)
        flank_xs = tuple(
            start[0]
            for start, end in zip(trunk_route.points, trunk_route.points[1:])
            if abs(start[0] - end[0]) <= COORD_TOLERANCE
            and abs(start[1] - end[1]) > COORD_TOLERANCE
        )
        if flank_xs:
            target_x = f.ctx.graph.stations[sibling.target].x
            sibling_flanks.append(min(flank_xs, key=lambda x: abs(x - target_x)))
    if not sibling_flanks:
        return geometry
    corner_x = max(
        geometry.corner_x,
        max(sibling_flanks)
        + cotravelling_lane_clearance(
            same_line=True,
            counter_running=True,
            curve_radius=f.ctx.curve_radius,
        ),
    )
    return _entry_wrap_record(
        f.ctx,
        f.edge,
        f.src,
        pos_n=geometry.pos_n,
        delta=geometry.delta,
        corner_x=corner_x,
        channel_y=geometry.channel_y,
        descent_x=geometry.descent_x,
    )


def _route_merge_trunk_around_below(f: _InterFacts) -> RoutedPath:
    """Loop a merge trunk under a target with no port-side channel."""
    shape = f.merge_trunk_shape
    assert shape.entry_port is not None
    return _emit_left_entry_wrap(
        f.edge,
        f.src,
        shape.entry_port,
        f.ctx,
        _merge_trunk_around_below_geometry(f, shape),
    )


def _bottom_row_climb_corridor_clear(
    graph: MetroGraph,
    src_row: int,
    tgt_row: int,
    src_col: int,
    tgt_col: int,
    *,
    max_content_row: int | None = None,
) -> bool:
    """Whether a bottommost-row source can climb to a higher-row target by
    running along its own row level instead of diving below it.

    True when the source sits in the bottommost content row, the target is in a
    higher row, and no same-row section occupies the columns the rightward run
    would cross.  In that case the intervening sections that classified the edge
    as a bypass are all in higher rows (above a run at the source's Y), so the
    canyon below the source row is clear and the dive is gratuitous.

    *max_content_row* lets a per-edge caller pass a single
    ``max_grid_row_with_content`` result rather than recomputing it each edge.
    """
    if max_content_row is None:
        max_content_row = max_grid_row_with_content(graph)
    if tgt_row >= src_row or src_row != max_content_row:
        return False
    return not _has_intervening_sections(graph, src_col, tgt_col, src_row)


def _is_row_level_bottom_row_climb(
    graph: MetroGraph,
    edge: Edge,
    src_row: int | None,
    tgt_row: int | None,
    src_col: int | None,
    tgt_col: int | None,
    *,
    max_content_row: int | None = None,
) -> bool:
    """Whether *edge* is a bottommost-row branch climbing to a higher-row entry
    port along a clear row-level corridor.

    True only when the edge lands on a real section entry port -- a merge/fan
    junction target collects feeders onto a shared trunk below the row instead --
    and :func:`_bottom_row_climb_corridor_clear` holds.  Such a branch keeps its
    traverse at the source's own Y rather than diving below it; bypass emission,
    the fan bypass band, and the row-level climb guards must all agree on which
    edges qualify, so they share this predicate.

    *max_content_row* is threaded through to
    :func:`_bottom_row_climb_corridor_clear` so a per-edge caller can reuse one
    ``max_grid_row_with_content`` result.
    """
    if src_row is None or tgt_row is None or src_col is None or tgt_col is None:
        return False
    tgt_port = graph.ports.get(edge.target)
    if tgt_port is None or not tgt_port.is_entry:
        return False
    return _bottom_row_climb_corridor_clear(
        graph, src_row, tgt_row, src_col, tgt_col, max_content_row=max_content_row
    )


@dataclass(frozen=True, slots=True)
class _BypassGeometry:
    """The U-shaped bypass centreline plus the source seam it opens on."""

    centreline: tuple[tuple[float, float], ...]
    sigma1: float
    sigma2: float
    src_bundle_offsets: tuple[float, ...]
    tgt_bundle_offsets: tuple[float, ...]
    gap1_x: float
    gap2_x: float
    gap1_vertical: Direction
    gap2_vertical: Direction
    g1_j: int
    g1_n: int
    g2_j: int
    g2_n: int
    seam: _SourceSeam


def _bypass_geometry(
    f: _InterFacts,
    shape: _MergeTrunkShape | None = None,
) -> _BypassGeometry:
    """Resolve the U-shaped bypass centreline around intervening sections.

    *shape* is the merge trunk's reading of the same U, which overrides the
    target coordinates for gap2 placement (the merge junction is virtual and
    sits at a different Y inside the section from the entry port the trunk
    actually reaches), asks ``bypass_bottom_y`` to route below ALL sections in
    the column range whatever rows the endpoints are in, and can pull gap2_x
    into the half of the inter-column gap AWAY from the target's edge so it
    does not overlap a sibling around-section route hugging that edge.  The
    pull-away is only honoured while it keeps gap2_x at least
    SECTION_ROUTE_CLEARANCE from the neighbouring section; otherwise the
    standard placement is used (the bundles will overlap, but the alternative
    is to put gap2_x INSIDE the neighbouring section bbox, which is worse).
    """
    edge, src, tgt, ctx = f.edge, f.src, f.tgt, f.ctx
    i, src_row = f.i, f.src_row
    assert f.src_col is not None and f.tgt_col is not None
    src_col, tgt_col = f.src_col, f.tgt_col
    effective_entry_side = f.effective_entry_side
    effective_tx = shape.effective_tx if shape is not None else None
    effective_ty = shape.effective_ty if shape is not None else None
    force_cross_row = shape is not None and shape.force_cross_row
    trunk_v_up_pull_away = shape is not None and shape.trunk_v_up_pull_away
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
    tgt_row = f.tgt_row
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
        reserved=ctx.reserved_bands.rows,
    )

    # A bottommost-row source climbing to a higher-row target keeps its run at
    # its own Y when the row level to the right is clear: the sections that
    # forced the bypass classification sit in higher rows, above this run, so
    # diving below the source row and climbing back up is a gratuitous dogleg.
    # A merge/fan junction target collects feeders onto a shared trunk below the
    # row, so this only applies to a route landing on a real section entry port.
    src_off = _get_offset(ctx, edge.source, edge.line_id)
    tgt_entry = graph.ports.get(edge.target)
    if cross_row and _is_row_level_bottom_row_climb(
        graph, edge, src_row, tgt_row, src_col, tgt_col
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
        and not f.h_segment_crosses_other_section(
            sx,
            effective_tx,
            sy,
            f.endpoint_section_ids,
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

    # A bypass branch of a junction fan traverses the fan's one shared below-row
    # band (the deepest sibling's ``bypass_bottom_y``), so its trunk coincides
    # with its bypass siblings' by construction.  Only ever lowers a shallower
    # branch onto the shared band -- never lifts one above a section it must
    # clear -- and the source-track special cases above keep precedence.  A feed
    # into a merge junction is excluded: its convergence shares the merge's own
    # ``trunk_by`` drop level, which a fan band would desync it from.
    if (
        fan is not None
        and base_y > sy + COORD_TOLERANCE
        and edge.target not in ctx.merge.junctions
    ):
        corridor = ctx.fan_corridors.get(edge.source)
        if (
            corridor is not None
            and corridor.bypass_band_y is not None
            and corridor.bypass_band_y >= base_y - COORD_TOLERANCE
        ):
            base_y = corridor.bypass_band_y

    # The step off the source leg down onto the trunk is two formed corners
    # with a vertical run between them, so it needs a full radius of runway at
    # each end.  A section bottom that leaves less than that -- the source
    # already sitting most of the way down to the traverse's clearance lane --
    # is deepened to exactly ``2 * curve_radius``, the shallowest step the
    # corners can be drawn at without the bundle builder halving their radius.
    src_leg_y = sy + src_off
    step_runway = 2 * ctx.curve_radius
    step = base_y + nest_offset - src_leg_y
    if COORD_TOLERANCE < step < step_runway - COORD_TOLERANCE:
        base_y = src_leg_y + step_runway - nest_offset

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
            ui, un = fan
            fan_delta = l_shape_stagger(ui, un, gap1_vertical, ctx.offset_step)
            fan_mid_x = _fan_corner_x(ctx, src, un, horizontal, facts=f)
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
    else:
        if fan is not None:
            # Wrap-style routes whose source-side curve is on the RIGHT
            # regardless of dx (left-entry wrap, around-section-below) are
            # dispatched through their own handlers, not here.
            ui, un = fan
            fan_delta = l_shape_stagger(ui, un, gap1_vertical, ctx.offset_step)
            fan_mid_x = _fan_corner_x(ctx, src, un, horizontal, facts=f)
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

    # gap2 descends on the entry's outward side: a LEFT entry is reached in the
    # gap left of the target, a RIGHT entry in the gap right of it, whichever way
    # the U travelled to get there.  A merge junction stands in front of a real
    # entry port, so it descends on that port's side too -- its own x sits inside
    # the target box, where a same-travel-direction descent would land the port
    # approach crossing the interior.  A source facing the wrong side of a plain
    # LEFT/RIGHT port is diverted to the wrap handlers before the U, so a direct
    # port's outward side matches the travel direction by construction here.
    gap2_left = effective_entry_side is PortSide.LEFT

    if gap2_left:
        gap2_base = _gap_channel_base(
            graph,
            tgt_col - 1,
            tgt_row,
            g2_n,
            ctx.offset_step,
            anchor_section_id=tgt_sec_id,
            anchor_side=PortSide.LEFT,
            anchor_is_target=True,
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
    else:
        gap2_base = _gap_channel_base(
            graph,
            tgt_col,
            tgt_row,
            g2_n,
            ctx.offset_step,
            anchor_section_id=tgt_sec_id,
            anchor_side=PortSide.RIGHT,
            anchor_is_target=True,
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
        else:
            g1_lo, g1_hi = column_gap_edges(graph, src_col - 1, src_col)
        if gap2_left:
            g2_lo, g2_hi = column_gap_edges(graph, tgt_col - 1, tgt_col)
        else:
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
                    gap1_x = max(
                        sx + ctx.curve_radius, src_right + SECTION_ROUTE_CLEARANCE
                    )
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
    # Gap-slot ranks follow channel travel, while the target rise's right-hand
    # normal points against that ordering.
    tgt_anchor = channel_fan(sigma2, g2_j, g2_n, -n3x)
    # The member's own lead-in and descent, not the centreline's: the emitted
    # leg leaves the source at its station lateral and drops in the channel
    # ``gap1_x`` names, which is where its opening corner stands.
    emitted_source_y = sy + src_off
    return _BypassGeometry(
        tuple(centerline),
        sigma1,
        sigma2,
        tuple(src_anchor),
        tuple(tgt_anchor),
        gap1_x,
        gap2_x,
        gap1_vertical,
        gap2_vertical,
        g1_j,
        g1_n,
        g2_j,
        g2_n,
        _SourceSeam(
            segment_direction((sx, emitted_source_y), (gap1_x, emitted_source_y)),
            segment_direction((gap1_x, emitted_source_y), (gap1_x, by)),
            sx,
            gap1_x,
        ),
    )


# The U-bypass leaves its source on the lead-in and turns down on the segment
# after it, which is the rank the reservation ledger and the opening-turn read.
BYPASS_DESCENT_RANK = 1


@dataclass
class _DescentMemo:
    """Resolved U-bypass readings, held for as long as they can be re-read.

    One reading is a full classify-and-place of the member's own shape, and the
    seating questions ask for every co-traveller's reading once per
    co-traveller, from the planner and again from the emitter. The reading
    depends on active plan queries and the routes constructed so far, so the
    memo is dropped when any of that state moves.
    """

    ctx: _RoutingCtx
    exit_turns: object
    convergences: object
    built_route_count: int
    by_edge: dict[EdgeKey, _BypassGeometry | None]

    def is_current_for(self, ctx: _RoutingCtx) -> bool:
        return (
            self.ctx is ctx
            and self.exit_turns is ctx.exit_turns
            and self.convergences is ctx.convergences
            and self.built_route_count == len(ctx.built_routes)
        )


_DESCENT_MEMO: _DescentMemo | None = None


def u_bypass_descent_geometry(edge: Edge, ctx: _RoutingCtx) -> _BypassGeometry | None:
    """The U-shaped bypass *edge*'s own handler builds, when it draws one."""
    global _DESCENT_MEMO
    memo = _DESCENT_MEMO
    if memo is None or not memo.is_current_for(ctx):
        memo = _DESCENT_MEMO = _DescentMemo(
            ctx,
            ctx.exit_turns,
            ctx.convergences,
            len(ctx.built_routes),
            {},
        )
    key = (edge.source, edge.target, edge.line_id)
    if key not in memo.by_edge:
        memo.by_edge[key] = _resolve_u_bypass_descent_geometry(edge, ctx)
    return memo.by_edge[key]


def _resolve_u_bypass_descent_geometry(
    edge: Edge, ctx: _RoutingCtx
) -> _BypassGeometry | None:
    """The U-shaped bypass *edge*'s own handler builds, when it draws one.

    Two families open on that U: the bypass family, and a merge trunk drawing
    it to the entry port standing behind its junction instead of to the
    junction itself.  They share the gap-1 channel a bundle is seated in, so
    one reading of the shape has to serve both -- and it has to be the reading
    the classified family's own reading gives, since a member claimed by an earlier
    rule claims draws no U at all.

    ``None`` where the member draws something else.
    """
    src, tgt = ctx.graph.edge_endpoints(edge)
    facts = _build_inter_facts(edge, src, tgt, ctx)
    if facts.src_col is None or facts.tgt_col is None:
        return None
    family = classify_inter_section_family(edge, src, tgt, ctx)
    if family is RouteFamilyId.MERGE_TRUNK:
        shape = _merge_trunk_shape(facts)
        if shape.around_below:
            return None
        return _bypass_geometry(facts, shape)
    if (
        family is not RouteFamilyId.BYPASS_FAMILY
        or _bypass_route_kind(facts) is not _BypassRoute.U_BYPASS
    ):
        return None
    return _bypass_geometry(facts)


def _bypass_descent_lanes(
    edge: Edge, ctx: _RoutingCtx
) -> list[tuple[EdgeKey, float]] | None:
    """Every co-travelling descent the reservation seats with this one.

    The seating pass translates a claimed group as a whole, so the group -- not
    the member -- names the column any one of its members lands on.  A member
    with no claim contributes no bound, which is how the first routing pass
    reads: it is the pass that publishes the ledger, so it has none to consult.

    ``None`` where a co-traveller does not resolve to a U-bypass descent at all:
    the group is then not this bundle, and no member's column follows from the
    bundle's own claims.
    """
    lanes: list[tuple[EdgeKey, float]] = []
    for member in ctx.graph.edges_from(edge.source):
        if member.target != edge.target:
            continue
        descent = u_bypass_descent_geometry(member, ctx)
        if descent is None:
            return None
        lanes.append(((member.source, member.target, member.line_id), descent.gap1_x))
    return lanes or None


class SeatedDescent(NamedTuple):
    """A U-bypass descent's seated column with its place in the seating group."""

    column: float
    rank: int
    width: int


def seated_bypass_descent(
    edge: Edge, geometry: _BypassGeometry, ctx: _RoutingCtx
) -> SeatedDescent | None:
    """This descent's seated column with its rank and width in the bundle.

    The handler places the descent from the grid edges it has to hand, and the
    member-geometry freeze then translates the whole claimed bundle into its
    reserved band.  Reading the same displacement here is what lets the emitted
    turn and the plan that names it stand on one column; ``None`` where the
    bundle's own claims do not name it.
    """
    lanes = _bypass_descent_lanes(edge, ctx)
    if lanes is None:
        return None
    columns = sorted({column for _key, column in lanes})
    travel = seat_bundle_in_claimed_bands(
        ctx.reserved_bands, lanes, rank=BYPASS_DESCENT_RANK
    )
    return SeatedDescent(
        geometry.gap1_x + travel, columns.index(geometry.gap1_x), len(columns)
    )


def seated_left_exit_under_target_descent(
    geometry: _LeftExitUnderTargetLoop, ctx: _RoutingCtx
) -> float:
    """This loop's descent column once the reservation seats its bundle.

    The loop places the descent from the grid edges it has to hand, and the
    member-geometry freeze then translates the whole claimed bundle into its
    reserved band.  Reading the same arithmetic here is what lets the emitted
    turn and the plan that names it stand on one column.
    """
    assert geometry.seam.axis_coordinate is not None
    return geometry.seam.axis_coordinate + seat_bundle_in_claimed_bands(
        ctx.reserved_bands, list(geometry.lane_columns), rank=BYPASS_DESCENT_RANK
    )


def _seat_left_exit_under_target_descent(
    route: RoutedPath,
    edge: Edge,
    geometry: _LeftExitUnderTargetLoop,
    ctx: _RoutingCtx,
) -> None:
    """Stack the built descent at its rank in the seated bundle.

    A planned member has no later pass to discover where the reservation seats
    its bundle, since the plan owns the segment from the moment it is bound.  A
    member with no plan keeps the drawn column, which the freeze then translates
    over the whole claimed bundle at once.
    """
    if ctx.exit_turns is None:
        return
    membership = ctx.exit_turns.membership_for_edge(edge)
    if (
        membership is None
        or membership.plan.disposition is not ExitTurnDisposition.PLANNED
    ):
        return
    start, end = route.points[BYPASS_DESCENT_RANK : BYPASS_DESCENT_RANK + 2]
    if abs(start[0] - end[0]) > COORD_TOLERANCE:
        return
    drawn_column = geometry.seam.axis_coordinate
    assert drawn_column is not None  # the loop always turns onto its descent
    columns = sorted({column for _key, column in geometry.lane_columns})
    _restack_channel(
        _VChannel(
            route,
            BYPASS_DESCENT_RANK,
            start[0],
            min(start[1], end[1]),
            max(start[1], end[1]),
            end[1] > start[1],
        ),
        seated_left_exit_under_target_descent(geometry, ctx),
        columns.index(drawn_column),
        len(columns),
        ctx.offset_step,
        ctx.curve_radius,
    )


def bypass_line_draws_a_chained_trunk(edge: Edge, ctx: _RoutingCtx) -> bool:
    """Whether this line reaches its own source section on a second U-bypass.

    Two U-bypasses chained through one section leave that line with two trunks
    that share a below-row channel.  Freezing either descent hands its column to
    the plan and takes it out of the movable population
    :func:`~nf_metro.layout.routing.normalize._materialize_gap_slots` centres,
    which seats the rest of the gap one step over; the descent columns that move
    are the X extents :func:`_group_channel_trunks` gathers a channel by, so the
    two chained trunks fall into separate groups and
    :func:`_pack_band_tracks` offers the track one holds to a sibling of the
    other.  Read from the graph rather than from trunk Ys so both routing passes
    answer alike.
    """
    section = ctx.graph.stations[edge.source].section_id
    return any(
        member.line_id == edge.line_id
        and (member.source, member.target) != (edge.source, edge.target)
        and ctx.graph.stations[member.target].section_id == section
        and u_bypass_descent_geometry(member, ctx) is not None
        for member in ctx.graph.edges
    )


def _seat_bypass_descent(
    route: RoutedPath, edge: Edge, geometry: _BypassGeometry, ctx: _RoutingCtx
) -> None:
    """Stack the built descent at its rank in the gap-1 bundle.

    The bundle's own rank is what makes the two corners flanking the descent
    concentric with their siblings', and the reserved band is where the freeze
    will seat it: a planned member has no later pass to discover either, since
    the plan owns the segment from the moment it is bound.  A member with no
    plan keeps the gap-edge derivation, which
    :func:`~nf_metro.layout.routing.normalize._materialize_gap_slots` then
    settles over the gap's whole population -- a wider view than one bundle's
    claims, so it is the better answer wherever it is still available.
    """
    if ctx.exit_turns is None:
        return
    membership = ctx.exit_turns.membership_for_edge(edge)
    if (
        membership is None
        or membership.plan.disposition is not ExitTurnDisposition.PLANNED
    ):
        return
    seated = seated_bypass_descent(edge, geometry, ctx)
    if seated is None:
        return
    start, end = route.points[BYPASS_DESCENT_RANK : BYPASS_DESCENT_RANK + 2]
    if abs(start[0] - end[0]) > COORD_TOLERANCE:
        return
    _restack_channel(
        _VChannel(
            route,
            BYPASS_DESCENT_RANK,
            start[0],
            min(start[1], end[1]),
            max(start[1], end[1]),
            end[1] > start[1],
        ),
        seated.column,
        seated.rank,
        seated.width,
        ctx.offset_step,
        ctx.curve_radius,
    )


def _route_bypass(f: _InterFacts, geometry: _BypassGeometry) -> RoutedPath:
    """Build the U-shaped bypass its resolved geometry describes."""
    edge, ctx = f.edge, f.ctx
    route = route_tapered_anchored(
        (edge, edge.line_id, geometry.sigma1, geometry.sigma2),
        list(geometry.centreline),
        transition_leg=3,
        base_radius=ctx.curve_radius,
        src_bundle_offsets=list(geometry.src_bundle_offsets),
        tgt_bundle_offsets=list(geometry.tgt_bundle_offsets),
        normalize_exempt=False,
    )
    _declare_channel(
        route,
        ctx,
        geometry.gap1_x,
        geometry.gap1_vertical,
        geometry.g1_j,
        geometry.g1_n,
    )
    if route is not None:
        _seat_bypass_descent(route, edge, geometry, ctx)
    _declare_channel(
        route,
        ctx,
        geometry.gap2_x,
        geometry.gap2_vertical,
        geometry.g2_j,
        geometry.g2_n,
    )
    # The two gap columns can resolve onto one leg when the U's mid jog runs the
    # same way as a gap channel, which leaves the other leg holding a gap it
    # never declared.
    _declare_placed_channels(route, ctx, geometry.g2_j, geometry.g2_n)
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


def _declare_placed_channels(
    route: RoutedPath | None,
    ctx: _RoutingCtx,
    slot_index: int = 0,
    n_slots: int = 1,
) -> None:
    """Declare every inter-column gap the built route's vertical legs occupy.

    :func:`_declare_channel` names the one leg a handler can point at by X and
    intended direction.  A handler that emits a whole frozen frame in one go has
    no such single leg: which of its legs land in a gap, and which way each
    runs, are properties of the geometry it just built.  Reading them back leg
    by leg is what lets that frame state its occupancy the way every other
    handler does, so :func:`_materialize_gap_slots` can seat the gap's movable
    bundles clear of it.  A leg outside every gap declares nothing, and a gap
    already declared for this direction is not declared twice, since one slot
    stands for the whole column -- including a slot a targeted
    :func:`_declare_channel` already put there, so the two can be combined to
    name the legs a handler points at and sweep up whatever else it built.
    """
    if route is None:
        return
    declared: set[tuple[int, int | None, Direction]] = {
        (slot.gap_lo_col, slot.row, slot.direction) for slot in route.gap_slots
    }
    for (x0, y0), (x1, y1) in zip(route.points, route.points[1:]):
        if abs(x1 - x0) > COORD_TOLERANCE or abs(y1 - y0) <= COORD_TOLERANCE:
            continue
        x, y_lo, y_hi = x0, min(y0, y1), max(y0, y1)
        match = gap_lo_for_x(ctx.graph, x, y_lo, y_hi)
        if match is None:
            continue
        lo, matched_row = match
        direction = Direction.D if y1 > y0 else Direction.U
        if (lo, matched_row, direction) in declared:
            continue
        declared.add((lo, matched_row, direction))
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


def _l_shape_mid_x(
    edge: Edge, src: Station, tgt: Station, n: int, ctx: _RoutingCtx
) -> float:
    """The vertical channel X the plain L-shape would use for this edge.

    Shared between :func:`_route_l_shape_plain` (which builds the route on
    it) and callers that need to know where that channel lands before
    committing to the L-shape at all.
    """
    sx, sy = src.x, src.y
    tx, ty = tgt.x, tgt.y
    dx = tx - sx

    max_r = outer_lane_radius(n, ctx.curve_radius, ctx.offset_step)
    mid_x = inter_column_channel_x(
        ctx.graph,
        src,
        tgt,
        sx,
        dx,
        max_r,
        ctx.offset_step,
        ctx.reserved_bands.columns,
    )
    half_width = bundle_width(n, ctx.offset_step) / 2
    return clear_channel_of_section_edge(
        ctx.graph,
        mid_x,
        half_width,
        min(sy, ty),
        max(sy, ty),
        endpoint_port_xs(ctx.graph, edge),
        target_x=tx,
    )


def _route_l_shape_plain(
    edge: Edge,
    src: Station,
    tgt: Station,
    n: int,
    ctx: _RoutingCtx,
    mid_x: float | None = None,
) -> RoutedPath | None:
    """L-shape for a self-contained bundle: centreline + tapering fan.

    One H -> V -> H centreline.  The source fan (an exit port / merge junction)
    and the target entry trunk can have different spreads, so the bundle tapers
    (each line lands on its own offset at both ends).  A vertical leg shorter
    than its two corners shrinks the base radius to fit.

    *mid_x* pins the vertical channel; when omitted it falls to the
    gap-centred default (:func:`_l_shape_mid_x`).
    """
    sy, ty = src.y, tgt.y
    if mid_x is None:
        mid_x = _l_shape_mid_x(edge, src, tgt, n, ctx)

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
    sy = src.y
    tx, ty = tgt.x, tgt.y
    geometry = _l_shape_fan_source_turn(edge, src, tgt, fan, ctx)
    ui, un = fan
    delta = l_shape_stagger(ui, un, geometry.turn_direction, ctx.offset_step)
    src_off = _get_offset(ctx, edge.source, edge.line_id)
    tgt_off = _get_offset(ctx, edge.target, edge.line_id)
    centerline = [
        (geometry.launch_x, sy + src_off + delta),
        (geometry.axis_x, sy + src_off + delta),
        (geometry.axis_x, ty + tgt_off + delta),
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
    _declare_channel(
        route,
        ctx,
        geometry.axis_x,
        geometry.turn_direction,
        ui,
        un,
    )
    return route


@dataclass(frozen=True, slots=True)
class _LShapeFanSourceTurn:
    """Source-side geometry shared by L-shape planning and emission."""

    launch_x: float
    axis_x: float
    run_direction: Direction
    turn_direction: Direction


def _l_shape_fan_source_turn(
    edge: Edge,
    src: Station,
    tgt: Station,
    fan: tuple[int, int],
    ctx: _RoutingCtx,
) -> _LShapeFanSourceTurn:
    """Resolve the fixed launch and turn column of a junction-fan L-shape."""
    sx, sy = src.x, src.y
    tx, ty = tgt.x, tgt.y
    run_direction = horizontal_direction(tx - sx)
    turn_direction = vertical_direction(ty - sy)
    _rank, size = fan
    half_width = (size - 1) * ctx.offset_step / 2
    axis_x = sx + run_direction.sign * (ctx.curve_radius + half_width)
    axis_x = clear_channel_of_section_edge(
        ctx.graph,
        axis_x,
        half_width,
        min(sy, ty),
        max(sy, ty),
        endpoint_port_xs(ctx.graph, edge),
        target_x=tx,
    )
    lead_length = ctx.curve_radius + 2 * half_width
    launch_x = axis_x - run_direction.sign * lead_length
    launch_x = min(launch_x, sx) if run_direction is Direction.R else max(launch_x, sx)
    return _LShapeFanSourceTurn(
        launch_x,
        axis_x,
        run_direction,
        turn_direction,
    )


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


@dataclass(frozen=True, slots=True)
class _PerpExitGeometry:
    points: tuple[tuple[float, float], ...]
    member_offset: float
    bundle_offsets: tuple[float, ...]
    target_offsets: tuple[float, ...] | None
    transition_leg: int | None
    aligned_drop: bool
    seam: _SourceSeam
    cross_lo: float
    cross_hi: float


def _perp_exit_record(
    points: tuple[tuple[float, float], ...],
    member_offset: float,
    bundle_offsets: tuple[float, ...],
    target_offsets: tuple[float, ...] | None,
    transition_leg: int | None,
    *,
    aligned_drop: bool,
) -> _PerpExitGeometry:
    """Complete a perpendicular-exit record from its centreline.

    The route leaves the port along its first leg and, where the centreline
    turns, runs the second leg across at ``axis_coordinate``.  A centreline
    whose turn leg has collapsed to a point states no turn: the emitted member
    is one straight vertical.
    """
    run_direction = segment_direction(points[0], points[1])
    assert run_direction is not None
    turn_direction = (
        segment_direction(points[1], points[2]) if len(points) >= 4 else None
    )
    if len(points) < 4:
        cross_lo = cross_hi = points[0][0]
    else:
        # The turn leg is horizontal, so each of its ends takes its X shift from
        # the vertical leg that meets it there (``bundle._right_normal``).
        second_run = segment_direction(points[2], points[3])
        assert second_run is not None
        cross_lo, cross_hi = sorted(
            (
                points[1][0] - member_offset * run_direction.sign,
                points[2][0] - member_offset * second_run.sign,
            )
        )
    return _PerpExitGeometry(
        points,
        member_offset,
        bundle_offsets,
        target_offsets,
        transition_leg,
        aligned_drop,
        _SourceSeam(
            run_direction,
            turn_direction,
            None if aligned_drop else points[0][1],
            None
            if turn_direction is None
            else points[1][1] + member_offset * turn_direction.sign,
        ),
        cross_lo,
        cross_hi,
    )


def _perp_exit_geometry(f: _InterFacts) -> _PerpExitGeometry | None:
    """Resolve the source seam shared by perpendicular-exit planning and emission.

    A TOP/BOTTOM exit on a horizontal-flow section either drops straight into a
    column-aligned TB/BT trunk (``aligned_drop``) or goes up and over the source
    section (:func:`_perp_exit_over_geometry`).  Returns ``None`` when *src* is
    not such an exit.
    """
    edge, src, tgt, ctx = f.edge, f.src, f.tgt, f.ctx
    src_port = f.src_port
    if (
        src_port is None
        or src_port.is_entry
        or src_port.side not in (PortSide.TOP, PortSide.BOTTOM)
        or src.section_id in ctx.tb_sections
    ):
        return None
    tgt_port = f.tgt_port
    aligned_drop = (
        tgt_port is not None
        and tgt_port.is_entry
        and tgt_port.side in (PortSide.TOP, PortSide.BOTTOM)
        and tgt.section_id in ctx.tb_sections
        and f.src_col == f.tgt_col
    )
    if not aligned_drop:
        return _perp_exit_over_geometry(edge, src, tgt, ctx)
    # The exit and the trunk below it share an X (``_align_drop_target_trunk``),
    # so the leg is one straight segment.  Each line drops at the target trunk's
    # per-line X offset, keeping a co-travelling bundle parallel down to the
    # port and on into the trunk, merging only at the first station inside it.
    drop_x = tgt.x + _tb_x_offset(ctx, edge.target, edge.line_id, tgt.section_id)
    return _perp_exit_record(
        ((drop_x, src.y), (drop_x, tgt.y)),
        0.0,
        (),
        None,
        None,
        aligned_drop=True,
    )


def _perp_exit_over_geometry(
    edge: Edge, src: Station, tgt: Station, ctx: _RoutingCtx
) -> _PerpExitGeometry:
    """Resolve the up-and-over centreline a perpendicular exit leaves on.

    ``member_offset`` is *edge*'s own lateral on the centreline's vertical legs;
    ``cross_lo``/``cross_hi`` bound the X the emitted turn leg spans once that
    lateral is applied.  See :func:`_route_perp_exit_over` for the shape.
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
        d = _perp_riser_lateral(ctx, edge.source, line_id, src_port.side)
        return d if is_top else -d

    src_offs = {lid: source_lateral(lid) for lid in line_ids}

    def inter_col_gap_x() -> float:
        """X of the gap between the source and target columns."""
        src_col = src_sec.grid_col if src_sec is not None else 0
        tgt_col = tgt_sec.grid_col if tgt_sec is not None else src_col
        return centre_inter_column_channel(
            graph, src_col, tgt_col, row, reserved=ctx.reserved_bands.columns
        )

    # Corridor Y: the header band is the clearance the lane nearest the section
    # owes its edge, and the bundle stacks from the centreline toward that edge,
    # so the whole ladder seats one bundle depth further out.  A run settled
    # after the fact would be pushed here anyway; a planned turn axis is frozen
    # against that settlement and so has to state it.
    toward_content = 1.0 if is_top else -1.0
    bundle_depth = max(
        (offset * toward_content for offset in src_offs.values()), default=0.0
    )
    cy_base = (
        header_corridor_y(graph, row, below=not is_top, base_radius=base, default=sy)
        if row is not None
        else sy - base
        if is_top
        else sy + base
    ) - toward_content * max(bundle_depth, 0.0)

    perp_entry = (
        tgt_port is not None
        and tgt_port.is_entry
        and tgt_port.side in (PortSide.TOP, PortSide.BOTTOM)
    )
    if not perp_entry:
        # Side entry: descend in the inter-column gap to the consumer's row and
        # turn straight in, holding each line on the target section's per-line Y
        # so the bundle stays stacked into the station marker rather than
        # collapsing onto the entry-port Y (which would hide all but one line).
        gap_x = inter_col_gap_x()
        return _perp_exit_record(
            ((sx, sy), (sx, cy_base), (gap_x, cy_base), (gap_x, ty), (tx, ty)),
            src_offs[edge.line_id],
            tuple(src_offs[lid] for lid in line_ids),
            tuple(_get_offset(ctx, edge.target, lid) for lid in line_ids),
            3,
            aligned_drop=False,
        )

    assert tgt_port is not None
    entry_above = tgt_port.side == PortSide.TOP
    crosses_box = (cy_base > ty) if entry_above else (cy_base < ty)
    if crosses_box:
        # The exit-side corridor sits on the far side of the target from its
        # entry port, so a straight descent on the trunk X would run up through
        # the target's stations.  Cross to the inter-column gap, switch to the
        # entry-side corridor outside the target box, then turn the final
        # perpendicular leg in from the port's own side.
        gap_x = inter_col_gap_x()
        # The exit-side down-leg drops at the exit X and runs across only to the
        # inter-column gap, so it need clear just the source column's sections,
        # not the row's deepest section in a far column (which would loop the leg
        # to the canvas bottom around a box it never passes under).
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
        points: tuple[tuple[float, float], ...] = (
            (sx, sy),
            (sx, cy_down),
            (gap_x, cy_down),
            (gap_x, cy_entry),
            (tx, cy_entry),
            (tx, ty),
        )
    else:
        # Perpendicular entry: descend straight on the target trunk's per-line X
        # and stop there.  The matching entry drop continues from that same X, so
        # ending the corridor short of the port centre keeps the two legs one
        # continuous line instead of jogging onto the port marker.
        points = ((sx, sy), (sx, cy_base), (tx, cy_base), (tx, ty))
    return _perp_exit_record(
        points,
        src_offs[edge.line_id],
        tuple(src_offs[lid] for lid in line_ids),
        None,
        None,
        aligned_drop=False,
    )


def _route_perp_exit(f: _InterFacts) -> RoutedPath | None:
    """Route a perpendicular (TOP/BOTTOM) exit on a horizontal-flow section.

    A column-aligned drop into a TB/BT trunk is a straight vertical; a side
    entry or a cross-column perpendicular entry goes up and over the source
    section.  Returns ``None`` when *src* is not such an exit.
    """
    edge, ctx = f.edge, f.ctx
    geometry = _perp_exit_geometry(f)
    if geometry is None:
        return None
    if geometry.aligned_drop:
        return _route_perp_exit_drop(edge, geometry, ctx)
    return _route_perp_exit_over(edge, geometry, ctx)


def _route_perp_exit_drop(
    edge: Edge, geometry: _PerpExitGeometry, ctx: _RoutingCtx
) -> RoutedPath | None:
    """Straight vertical drop from a perpendicular exit into an aligned entry.

    A TOP/BOTTOM exit on a horizontal-flow section and the TOP/BOTTOM entry it
    feeds share an X (the target trunk is aligned to the exit), so the
    inter-section leg is a single straight segment.
    """
    return route_along(
        edge,
        [(edge, edge.line_id, geometry.member_offset)],
        list(geometry.points),
        base_radius=ctx.curve_radius,
    )


def _route_perp_exit_over(
    edge: Edge, geometry: _PerpExitGeometry, ctx: _RoutingCtx
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
    if geometry.target_offsets is None:
        route = route_along(
            edge,
            [(edge, edge.line_id, geometry.member_offset)],
            list(geometry.points),
            base_radius=ctx.curve_radius,
            bundle_offsets=list(geometry.bundle_offsets),
        )
    else:
        assert geometry.transition_leg is not None
        target_offset = _get_offset(ctx, edge.target, edge.line_id)
        routes = build_tapered_bundle(
            [(edge, edge.line_id, geometry.member_offset, target_offset)],
            list(geometry.points),
            transition_leg=geometry.transition_leg,
            base_radius=ctx.curve_radius,
            bundle_offsets=list(
                zip(geometry.bundle_offsets, geometry.target_offsets, strict=True)
            ),
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
        and abs(graph.station_for_edge_target(sib).x - src.x) <= ctx.curve_radius
        for sib in graph.edges_from(edge.source)
    )
    if not aligned_sibling:
        return False
    exclude = {sid for sid in (src.section_id, tgt.section_id) if sid is not None}
    if _h_segment_crosses_other_section(graph, src.x, final_x, src.y, exclude):
        return False
    return not _v_segment_crosses_other_section(graph, final_x, src.y, tgt.y, exclude)


def _corridor_riser_x(
    ctx: _RoutingCtx, src_sec: Section | None, tgt_sec: Section | None
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
    return centre_inter_column_channel(
        ctx.graph,
        src_sec.grid_col,
        tgt_sec.grid_col,
        row=src_sec.grid_row,
        reserved=ctx.reserved_bands.columns,
    )


def _flanked_cross_row_riser_seat_x(
    ctx: _RoutingCtx,
    src: Station,
    tgt: Station,
    src_sec: Section | None,
    tgt_sec: Section | None,
    exit_side: Direction,
    run_start: float,
    run_end: float,
) -> float | None:
    """Riser X for a same-column cross-row perpendicular entry flanked on its exit side.

    A LEFT/RIGHT exit dropping into a TOP/BOTTOM entry stacked in its own grid
    column runs its riser in the inter-column gap on the exit side.  When a
    section occupies that neighbouring column at the source's own row the gap is
    walled on both sides, so the minimal lead-in -- a curve radius off the source
    box -- climbs the outside of that box.  Seat the riser
    :data:`EDGE_TO_BUNDLE_CLEARANCE` off the wall it exits instead, held inside
    the clearance its corridor owes over the whole descent, which leaves the rest
    of the (widened) gap for the counter-running bundles that share it.

    Returns ``None`` when the two boxes are not a same-column cross-row pair or
    the neighbouring column is open at the source row (the unflanked riser keeps
    its minimal off-wall lead-in).
    """
    if src_sec is None or tgt_sec is None or src_sec.grid_col != tgt_sec.grid_col:
        return None
    neighbour = src_sec.grid_col + (1 if exit_side is Direction.R else -1)
    if not any(
        other.grid_col == neighbour and other.grid_row == src_sec.grid_row
        for other in ctx.graph.sections.values()
    ):
        return None
    left, right = column_gap_edges(
        ctx.graph, src_sec.grid_col, neighbour, row=src_sec.grid_row
    )
    seat = (
        left + EDGE_TO_BUNDLE_CLEARANCE
        if exit_side is Direction.R
        else right - EDGE_TO_BUNDLE_CLEARANCE
    )
    return seat_run_in_corridor_clearance(
        ctx.graph,
        axis=0,
        section_ids=section_ids_of_stations(ctx.graph, src, tgt),
        coordinate=seat,
        run_start=run_start,
        run_end=run_end,
    )


def _top_entry_above_channel_y(ctx: _RoutingCtx, tgt_sec: Section) -> float:
    """Y of the routing channel just above a TOP-entry target's header band.

    A TOP entry is reached from above, so the drop into the port departs from a
    channel that clears the target row's header band (and, for the topmost row,
    the canvas title band).  When that band over-reserves -- a section merely
    exists somewhere in the row above so the full inter-row clearance applies
    even though nothing sits over the target's own column -- the channel is
    pulled down to just clear the target's own header badge, so the approach
    doesn't overshoot far past the port before turning back down.
    """
    corridor_y = header_corridor_y(
        ctx.graph,
        tgt_sec.grid_row,
        below=False,
        base_radius=ctx.curve_radius,
        default=tgt_sec.bbox_y,
    )
    badge_clear_y = (
        section_header_top(tgt_sec) - NEXT_ROW_HEADER_BADGE_CLEARANCE - ctx.curve_radius
    )
    return max(corridor_y, badge_clear_y)


def _top_entry_below_wrap_riser_x(
    src: Station,
    tgt: Station,
    final_x: float,
    above_y: float,
    ctx: _RoutingCtx,
) -> float | None:
    """Riser X to route a below-row feeder around a section into its TOP port.

    A TOP entry port sits on the target's top edge, so it must be entered from
    above.  When the feeder's source section is in a lower grid row than the
    target the inter-row channel lies *below* the target, and a straight rise
    into the port would plough up through the box interior, striking the
    interior station and its label (#1522).  The leg instead carries past one
    vertical side of the box, rises to the channel above it, then comes back
    over the top into the port.

    Returns the X of that riser -- outside the box on a side clear of every
    other section, preferring the side away from the feeder's approach so the
    leg carries past the box and returns over the top rather than re-crossing a
    near-side approach.  Returns ``None`` when the feeder is level with or above
    the target (the ordinary from-above approach), or when neither side is
    clear (the fall-through route then handles it and the runtime guard stays
    the backstop).
    """
    graph = ctx.graph
    src_sec = resolve_section(graph, src)
    tgt_sec = resolve_section(graph, tgt)
    if src_sec is None or tgt_sec is None or src_sec.grid_row <= tgt_sec.grid_row:
        return None

    exclude = {sid for sid in (src.section_id, tgt.section_id) if sid is not None}
    clearance = ctx.curve_radius + SECTION_ROUTE_CLEARANCE
    box_left = tgt_sec.bbox_x
    box_right = tgt_sec.bbox_x + tgt_sec.bbox_w
    right_x = col_right_edge(graph, tgt_sec.grid_col, default=box_right) + clearance
    left_x = col_left_edge(graph, tgt_sec.grid_col, default=box_left) - clearance
    # Prefer the side away from the feeder so the leg carries past the box and
    # returns over the top rather than re-crossing a near-side approach.
    prefer_right = src.x <= (box_left + box_right) / 2
    ordered = [right_x, left_x] if prefer_right else [left_x, right_x]

    def is_clear(rx: float) -> bool:
        return not (
            _h_segment_crosses_other_section(graph, src.x, rx, src.y, exclude)
            or _v_segment_crosses_other_section(graph, rx, src.y, above_y, exclude)
            or _h_segment_crosses_other_section(graph, rx, final_x, above_y, exclude)
        )

    return next((rx for rx in ordered if is_clear(rx)), None)


def _perp_entry_junction_straight_drop(
    edge: Edge, src: Station, tgt: Station, ctx: _RoutingCtx
) -> RoutedPath | None:
    """A junction feeding a perpendicular (TOP/BOTTOM) entry directly in line
    with it (shared X) travels straight into the port with no fan.

    The junction stands off-box in the inter-section gap; when its target
    port sits directly above or below it, the line travels that column with
    no fan -- a 2-point vertical.  This avoids the lead-out-and-jog the offset
    machinery otherwise stitches when the landing column coincides with the
    lead-in: a lateral out-and-back straddling the boundary.  Running a curve
    radius outside a flanking box wall is adequate clearance, not a reason to
    keep the jog; ``check_no_riser_hugs_section_edge`` exempts this
    junction-fed leg so the near-wall run is not rejected as a wall-hug.

    The column is the port's own per-line crossing
    (:func:`_perp_entry_landing_x`) and the drop ends on the port's edge, which
    is where every sibling feeding that port lands and where its intra-section
    departure leaves from.  Carrying a lane offset along the axis the drop
    travels instead would run this line past the boundary it is crossing, on a
    column none of its siblings stand in.

    Returns ``None`` when this shortcut doesn't apply, so the caller
    continues with the ordinary lead-in.
    """
    if abs(tgt.x - src.x) > COORD_TOLERANCE or src.id not in ctx.graph.junctions:
        return None
    sy = src.y
    tx, ty = tgt.x, tgt.y
    src_off = _get_offset(ctx, edge.source, edge.line_id)
    crossing_x = _perp_entry_landing_x(
        ctx, edge, resolve_section(ctx.graph, tgt), tx, edge.line_id
    )
    column = tx if crossing_x is None else crossing_x
    drop = route_along(
        edge,
        [(edge, edge.line_id, 0.0)],
        [(column, sy + src_off), (column, ty)],
        base_radius=ctx.curve_radius,
        normalize_exempt=True,
    )
    assert drop is not None
    membership = (
        ctx.exit_turns.membership_for_edge(edge) if ctx.exit_turns is not None else None
    )
    if membership is not None and membership.axis is not None:
        feeders = (
            ctx.graph.station_for_edge_source(item)
            for item in ctx.graph.edges_to(src.id)
        )
        feeder = min(
            (
                station
                for station in feeders
                if abs(station.y - src.y) <= COORD_TOLERANCE
                and abs(station.x - src.x) > COORD_TOLERANCE
            ),
            key=lambda station: abs(station.x - src.x),
            default=None,
        )
        if feeder is not None:
            x0, y0 = drop.points[0]
            side = -1.0 if feeder.x < src.x else 1.0
            lead = min(ctx.curve_radius, abs(feeder.x - src.x))
            drop.points = [(x0 + side * lead, y0), *drop.points]
    return drop


def _perp_entry_finish_route(
    edge: Edge,
    geometry: _PerpEntryLGeometry,
    ctx: _RoutingCtx,
) -> RoutedPath:
    """Fan a perpendicular-entry centreline into the route for *edge*'s line."""
    centerline = list(geometry.points)
    members = list(geometry.members)
    if geometry.fan_source_offsets is not None:
        # Anchor the source-region legs on the branch's own station offset -- the
        # lane it rides down the shared junction trunk -- so the lead-in leaves
        # the junction collinear with that trunk (no peel-off stub), and anchor
        # the concentric first corner against the whole fan's offsets so the
        # branch nests with its off-edge siblings.  Symmetric fan_offsets would
        # re-centre the branch on the fan's mean, parting it from its trunk lane
        # by half a step at the junction.
        member = members[0]
        route = route_tapered_anchored(
            member,
            centerline,
            transition_leg=geometry.transition_leg,
            base_radius=ctx.curve_radius,
            src_bundle_offsets=list(geometry.fan_source_offsets),
            tgt_bundle_offsets=[member[3]],
            normalize_exempt=True,
        )
        if geometry.flanked_gap:
            _declare_placed_channels(route, ctx)
        return route

    routes = build_tapered_bundle(
        members,
        centerline,
        geometry.transition_leg,
        base_radius=ctx.curve_radius,
        normalize_exempt=True,
    )
    route = next(r for r in routes if r.line_id == edge.line_id)
    if geometry.flanked_gap:
        # The riser descends a two-walled gap it counter-runs other bundles in.
        # Declaring its channel makes the exempt frame a visible obstacle so the
        # gap passes seat those movable bundles clear of it.
        _declare_placed_channels(route, ctx)
    return route


def _perp_entry_lands_on_its_own_lane(tgt_sec: Section) -> bool:
    """Whether a perpendicular entry lands each line on its own trunk lane.

    A section stacking its lines along X separates them across the very axis a
    TOP/BOTTOM port is approached along, so an arriving line meets the port
    already on the lane it will travel and flows straight on.  A section stacking
    along Y separates them across the port's own axis instead: every line
    converges on the shared port and the fan is re-formed behind it.

    Both the landing coordinate and the bundle's centreline turn on this, so they
    ask it here rather than each deciding for itself; a bundle centred on a lane
    the landing does not use draws the boundary jitter it exists to remove.
    """
    return lanes_run_along_x(tgt_sec.direction)


def _perp_entry_landing_x(
    ctx: _RoutingCtx,
    edge: Edge,
    tgt_sec: Section | None,
    tx: float,
    line_id: str,
) -> float | None:
    """The X at which *line_id* crosses *edge*'s perpendicular (TOP/BOTTOM) entry.

    Into a section that lands the line on its own trunk lane
    (:func:`_perp_entry_lands_on_its_own_lane`) that lane is the crossing.
    Otherwise it is the port-crossing X the intra-section drop departs from
    (:func:`_perp_entry_crossing_x`).  ``None`` where no bundled feeder reaches
    the port and the crossing is undefined.

    A merge feeder's boundary is the port the merge feeds, not the merge station
    standing on that port's lead-in.
    """
    if tgt_sec is not None and _perp_entry_lands_on_its_own_lane(tgt_sec):
        return tx + _tb_x_offset(ctx, edge.target, line_id, tgt_sec.id)
    entry_port_id = ctx.merge.entry_port_for.get(edge.target, edge.target)
    return _perp_entry_crossing_x(ctx, entry_port_id, line_id, tx)


def _perp_entry_bundle_members(
    edge: Edge,
    tgt_sec: Section | None,
    tx: float,
    ref_lid: str,
    line_ids: list[str],
    edge_by_line: dict[str, Edge],
    *,
    src_geom: Callable[[str], float],
    ctx: _RoutingCtx,
    side: PortSide,
) -> tuple[float, list[tuple[Edge, str, float, float]]]:
    """Landing X and per-line bundle members for a perpendicular (TOP or
    BOTTOM) entry port, shared by :func:`_route_top_entry_l_shape` and
    :func:`_route_bottom_entry_l_shape`.

    Every line lands on the X :func:`_perp_entry_landing_x` states for it, and
    the members carry that X as the lateral the bundle builder needs, so the
    approach and the departure meet as one stroke at the boundary however many
    lines share the bundle.  Landing on the arriving fan instead bakes each
    line's inbound offset into its lane, which for a line the section draws on
    the other side of the port is the boundary jitter: the stroke steps sideways
    as it crosses the section edge.  The source offset stands only where the
    crossing is undefined.

    Where the entry lands each line on its own lane
    (:func:`_perp_entry_lands_on_its_own_lane`) the reference line's lane is the
    centreline, so the target spread is the landing section's rather than the
    source fan's and the bundle tapers; otherwise the centreline stays on the
    port.
    """
    # The bundle builder fans a leg along its own right-hand normal, and the
    # landing leg descends into a TOP port but ascends into a BOTTOM one, so
    # one landing X reads as opposite laterals on the two sides.
    landing_normal_x = -1.0 if side is PortSide.TOP else 1.0

    def landing_x(line_id: str) -> float | None:
        return _perp_entry_landing_x(ctx, edge, tgt_sec, tx, line_id)

    if tgt_sec is not None and _perp_entry_lands_on_its_own_lane(tgt_sec):
        ref_landing_x = landing_x(ref_lid)
        assert ref_landing_x is not None
        final_x = ref_landing_x
    else:
        final_x = tx

    def tgt_offset(line_id: str) -> float:
        lx = landing_x(line_id)
        return src_geom(line_id) if lx is None else landing_normal_x * (lx - final_x)

    members = [
        (edge_by_line[lid], lid, src_geom(lid), tgt_offset(lid)) for lid in line_ids
    ]
    return final_x, members


@dataclass(frozen=True, slots=True)
class _PerpEntryLGeometry:
    points: tuple[tuple[float, float], ...]
    members: tuple[tuple[Edge, str, float, float], ...]
    transition_leg: int
    fan_source_offsets: tuple[float, ...] | None
    seam: _SourceSeam
    flanked_gap: bool = False


def _leg_direction(start: tuple[float, float], end: tuple[float, float]) -> Direction:
    """The heading of an axis-aligned leg, read from its own two endpoints.

    A caller that can legitimately reach coincident endpoints resolves the
    degenerate leg itself (a turn-less seam, say) before asking here; reaching
    this with a zero-length leg is an unmodelled geometry, so it fails loud with
    the endpoints named.
    """
    direction = segment_direction(start, end)
    if direction is None:
        from nf_metro.layout.routing.exit_turns import ExitTurnInvariantError

        raise ExitTurnInvariantError(
            f"zero-length route leg {start} -> {end}: cannot read a heading from "
            "coincident endpoints"
        )
    return direction


def _perp_entry_l_record(
    points: tuple[tuple[float, float], ...],
    members: tuple[tuple[Edge, str, float, float], ...],
    transition_leg: int,
    line_id: str,
    fan_source_offsets: tuple[float, ...] | None,
    flanked_gap: bool = False,
) -> _PerpEntryLGeometry:
    """Complete a perpendicular-entry record from its centreline.

    The route leads out horizontally and turns onto the landing column at
    ``axis_coordinate``.  A centreline that reaches that column with no lead-in
    states no turn: the emitted member is one straight vertical, and its run is
    the drop itself.

    The turn leg is vertical, so it takes its X shift from the offset the
    bundle builder fans it by -- the source offset while the taper lies ahead
    of it, the target offset once it is the transition leg itself.
    """
    if len(points) < 3:
        return _PerpEntryLGeometry(
            points,
            members,
            transition_leg,
            fan_source_offsets,
            _SourceSeam(_leg_direction(points[0], points[-1]), None, None, None),
            flanked_gap,
        )
    source_offset, target_offset = next(
        (source, target)
        for _member_edge, member_line, source, target in members
        if member_line == line_id
    )
    member_offset = source_offset if transition_leg > 1 else target_offset
    turn_direction = _leg_direction(points[1], points[2])
    return _PerpEntryLGeometry(
        points,
        members,
        transition_leg,
        fan_source_offsets,
        _SourceSeam(
            _leg_direction(points[0], points[1]),
            turn_direction,
            points[0][0],
            points[1][0] + right_normal_axis_sign(turn_direction) * member_offset,
        ),
        flanked_gap,
    )


def _perp_entry_seated_corridor(
    ctx: _RoutingCtx,
    src: Station,
    tgt: Station,
    coordinate: float,
    lane_offsets: tuple[float, ...],
    *,
    axis: int,
    run_start: float,
    run_end: float,
) -> float:
    """*coordinate* moved the least distance that seats every lane in its clearance.

    A planned turn axis is frozen against the settlement that holds a drawn run
    inside the clearance its corridor owes, so the centreline has to state that
    seat itself.  The clearance is owed per lane and the bundle fans about the
    centreline, so the seat is the least shift lying inside every lane's own
    band.  Returns *coordinate* where a lane names no corridor, or where no one
    shift satisfies them all: the corridor is then narrower than its lanes ask
    of it, which is the closing guard's to report rather than this to pick a
    side of.
    """
    section_ids = section_ids_of_stations(ctx.graph, src, tgt)
    if not section_ids:
        return coordinate
    lower: float | None = None
    upper: float | None = None
    for offset in lane_offsets:
        band = corridor_clearance_band(
            ctx.graph,
            axis=axis,
            section_ids=section_ids,
            coordinate=coordinate + offset,
            run_start=run_start,
            run_end=run_end,
        )
        if band is None:
            return coordinate
        lane_lower = band.lo - coordinate - offset
        lane_upper = band.hi - coordinate - offset
        lower = lane_lower if lower is None else max(lower, lane_lower)
        upper = lane_upper if upper is None else min(upper, lane_upper)
    if lower is None or upper is None or lower > upper + COORD_TOLERANCE_FINE:
        return coordinate
    return coordinate + (lower if lower > 0 else min(upper, 0.0))


def _perp_entry_channel_y(ctx: _RoutingCtx, tgt_sec: Section, side: PortSide) -> float:
    """Y of the routing channel outside the box edge a perpendicular entry sits on."""
    if side is PortSide.TOP:
        return _top_entry_above_channel_y(ctx, tgt_sec)
    return _bottom_entry_below_channel_y(ctx, tgt_sec)


def _perp_entry_l_geometry(
    edge: Edge,
    src: Station,
    tgt: Station,
    n: int,
    ctx: _RoutingCtx,
    side: PortSide,
    channel_y: float | None = None,
    *,
    planned: bool = False,
) -> _PerpEntryLGeometry | None:
    """Resolve the centreline shared by perpendicular-entry planning and emission.

    A short horizontal lead-in lets the transition from any preceding horizontal
    edge (e.g. exit -> junction) curve smoothly into a vertical drop, then a
    trunk run in the inter-row gap outside the target section turns cleanly into
    the port::

        (sx,sy) -> (lx, sy) -> (lx, hy) -> (tx, hy) -> (tx, ty)

    That is the bundle's reference centreline; every co-travelling line is fanned
    as a per-leg offset of it (rigid for an LR/RL drop, tapering into a TB/BT
    trunk), mirroring how LEFT entry ports receive a vertical run in the
    inter-column gap.

    The shape is chosen from, in order: wrapping past the box when the feeder
    approaches from the entry's far side, a direct traverse-then-turn when a
    junction fan branch can clear every other section, a collapsed drop when the
    lead-in already reaches the landing column, else the full staircase.

    *channel_y* pins ``hy``; without it the channel is derived from the gap the
    source's own row names, which a source standing off the section grid does
    not have.  Returns ``None`` for a junction standing in the port's own
    column, whose straight drop (:func:`_perp_entry_junction_straight_drop`)
    carries no fan to lay a centreline for.

    *planned* says an exit-turn plan owns the source turn, which is what
    decides whether the corridors are settled after the fact or stated here.
    """
    if abs(tgt.x - src.x) <= COORD_TOLERANCE and src.id in ctx.graph.junctions:
        return None
    is_top = side is PortSide.TOP
    sx, sy = src.x, src.y
    tx, ty = tgt.x, tgt.y
    dx = tx - sx
    dy = ty - sy

    # Y for the horizontal trunk channel in the inter-row gap.
    mid_y = channel_y
    if mid_y is None:
        mid_y = inter_row_channel_y(
            ctx.graph,
            src,
            tgt,
            sy,
            ty,
            dy,
            ctx.curve_radius,
            reserved=ctx.reserved_bands.rows,
        )

    # For a same-row cross-column producer the generic fallback in
    # inter_row_channel_y places the channel inside the section bbox, on the
    # wrong side of the boundary for a port that must be approached from
    # outside it; :func:`_perp_entry_channel_y` gives the safe channel beyond
    # the target's own edge.
    src_sec = resolve_section(ctx.graph, src)
    tgt_sec = resolve_section(ctx.graph, tgt)
    if (
        channel_y is None
        and src_sec is not None
        and tgt_sec is not None
        and src_sec.grid_row == tgt_sec.grid_row
        and ((mid_y > ty) if is_top else (mid_y < ty))
    ):
        mid_y = _perp_entry_channel_y(ctx, tgt_sec, side)

    # A multi-line bundle fans the channel toward the source box (the line
    # nearest it sits a bundle-width off the centre); keep the centre far enough
    # away that even that line clears the source section's facing edge.
    if (
        channel_y is None
        and n > 1
        and src_sec is not None
        and tgt_sec is not None
        and src_sec.grid_row != tgt_sec.grid_row
    ):
        max_off = (n - 1) * ctx.offset_step
        clear_of_source = (
            src_sec.bbox_y + src_sec.bbox_h + INTER_ROW_EDGE_CLEARANCE + max_off
            if is_top
            else src_sec.bbox_y - INTER_ROW_EDGE_CLEARANCE - max_off
        )
        mid_y = held_in_reserved_band(
            max(mid_y, clear_of_source) if is_top else min(mid_y, clear_of_source),
            reserved_row_band_between(
                ctx.reserved_bands.rows, src_sec.grid_row, tgt_sec.grid_row
            ),
        )

    # Horizontal lead-in: a short run so the corner from horizontal to
    # vertical gets a proper curve.  The line leaves the source on the side
    # it physically exits from (a right/left exit port, or a junction fed by
    # one): a right exit whose target trunk sits to its LEFT must clear the
    # source section on the right and double back over the inter-row gap (a
    # right-down-left-down shape), so following dx would turn the line back
    # across the source box.  Falls back to dx for sources with no horizontal
    # exit side, and to the upstream-feeder direction for near-vertical
    # junction sources.  A junction fed straight from directly in line carries
    # no horizontal travel, so its drop stays in the column with no lead-in: a
    # jog there would reverse lateral direction at the entry boundary.
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
                js = ctx.graph.station_for_edge_source(je)
                if js.is_port:
                    if abs(js.x - src.x) <= COORD_TOLERANCE:
                        straight_drop = True
                    else:
                        lead = Direction.R if js.x < src.x else Direction.L
                    break

    # The lead-in run covers the widest lane's arc, not just the base radius:
    # every lane of the bundle turns down through this corner, and a run sized to
    # the base radius clamps all of them to it.
    lead_run = outer_lane_radius(n, ctx.curve_radius, ctx.offset_step)
    lx0 = sx if straight_drop else sx + lead.sign * lead_run

    # A horizontal exit whose minimal lead-in would seat the vertical trunk hard
    # against the source box's exit edge runs the riser up that edge.  A same-row
    # pair centres the riser in the clear inter-column corridor; a same-column
    # cross-row pair flanked on its exit side seats it a clearance off the exit
    # wall, leaving the widened gap for the bundles it counter-runs.
    flanked_gap = False
    if exit_side is not None and not straight_drop:
        corridor_x = _corridor_riser_x(ctx, src_sec, tgt_sec)
        if corridor_x is None:
            corridor_x = _flanked_cross_row_riser_seat_x(
                ctx, src, tgt, src_sec, tgt_sec, exit_side, sy, mid_y
            )
            flanked_gap = corridor_x is not None
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

    # The bundle builder fans each source-region leg by the right-hand normal of
    # its travel direction, so a LEFT exit -- whose lead-in departs leftward --
    # seats the fan on the opposite side of the port from the section's own +Y
    # lane draw.  That parts the inter-section departure from the intra trunk by
    # twice the offset right at the boundary.  Signing the source offset by the
    # exit lead lands the departure on the section's lane whichever side the line
    # leaves from; a RIGHT exit (and a junction source, whose off-box point has
    # no intra trunk to meet) keep the raw offset.
    src_sign = lead.sign if exit_side is not None and src.is_port else 1.0

    def src_geom(line_id: str) -> float:
        return src_sign * src_offset(line_id)

    # A branch of a junction fan consumes the fan's shared per-line rank so its
    # source-side first corner coincides with the bypass/wrap siblings' rather
    # than seating an independent lead-in column that the normalize stack then
    # has to reconcile.  Scoped to a single-line fan edge (the branch peels its
    # own line to its own target); a multi-line perpendicular entry keeps the
    # co-travelling bundle build (its fan is the edge's own lines, not the
    # junction's).
    fan = ctx.junction_fan_info.get((edge.source, edge.target, edge.line_id))
    fan_single = fan if fan is not None and len(line_ids) == 1 else None
    fan_source_offsets: tuple[float, ...] | None = None
    if fan_single is not None:
        _pos_i, pos_n = fan_single
        corridor = ctx.fan_corridors.get(edge.source)
        if channel_y is None and corridor is not None and corridor.band_y is not None:
            # Drop into the fan's shared traverse band, so this branch and its
            # wrap siblings turn at one Y rather than a few px apart.
            mid_y = corridor.band_y
        if not straight_drop:
            lx0 = sx + lead.sign * _fan_corner_run(ctx, pos_n)
            if lead is Direction.R:
                lx0 = _v1_corner_x(ctx, src, sx, lx0)
        fan_source_offsets = tuple(
            sorted(
                _get_offset(ctx, edge.source, lid)
                for lid in ctx.graph.station_lines(edge.source)
            )
        )

    final_x, members = _perp_entry_bundle_members(
        edge,
        tgt_sec,
        tx,
        ref_lid,
        line_ids,
        edge_by_line,
        src_geom=src_geom,
        ctx=ctx,
        side=side,
    )

    # A feeder standing on the far side of the target from its port cannot turn
    # straight in without ploughing through the box; carry past one side, cross
    # to the channel beyond the port's own edge, and come back in over it.
    boundary_y = (
        _perp_entry_channel_y(ctx, tgt_sec, side) if tgt_sec is not None else ty
    )
    if straight_drop:
        wrap_x = None
    elif is_top:
        wrap_x = _top_entry_below_wrap_riser_x(src, tgt, final_x, boundary_y, ctx)
    else:
        wrap_x = _bottom_entry_above_wrap_riser_x(src, tgt, final_x, boundary_y, ctx)

    points: tuple[tuple[float, float], ...]
    if wrap_x is not None:
        points = (
            (sx, sy),
            (wrap_x, sy),
            (wrap_x, boundary_y),
            (final_x, boundary_y),
            (final_x, ty),
        )
        transition_leg = 3
    elif _top_entry_side_fan_traverse_is_clear(edge, src, tgt, final_x, ctx):
        points = ((sx, sy), (final_x, sy), (final_x, ty))
        transition_leg = 1
    # The lead-in already reaches the landing column, so the channel leg
    # between them is too short to turn through: run the lead-in to that column
    # and turn down once.  Turning down beside the column and jogging across at
    # the port would step the line sideways right on the boundary, which the
    # intra-section departure leaves at the landing column.  A source standing
    # in that column has no lead-in to run either, and only the drop is left.
    elif abs(lx0 - final_x) <= ctx.curve_radius:
        lead_in = () if abs(final_x - sx) <= COORD_TOLERANCE else ((final_x, sy),)
        points = ((sx, sy), *lead_in, (final_x, ty))
        transition_leg = len(lead_in)
    else:
        if planned:
            # A planned turn axis, and the channel leg hanging off it, are
            # frozen against the settlement that holds a drawn run inside its
            # corridor's clearance, so the centreline states that seat itself.
            lane_offsets = tuple(src_geom(lid) for lid in line_ids)
            riser_sign = right_normal_axis_sign(_leg_direction((sx, sy), (sx, mid_y)))
            lx0 = _perp_entry_seated_corridor(
                ctx,
                src,
                tgt,
                lx0,
                tuple(riser_sign * offset for offset in lane_offsets),
                axis=0,
                run_start=sy,
                run_end=mid_y,
            )
            channel_direction = segment_direction((lx0, mid_y), (final_x, mid_y))
            assert channel_direction is not None
            channel_sign = right_normal_axis_sign(channel_direction)
            mid_y = _perp_entry_seated_corridor(
                ctx,
                src,
                tgt,
                mid_y,
                tuple(channel_sign * offset for offset in lane_offsets),
                axis=1,
                run_start=lx0,
                run_end=final_x,
            )
        points = (
            (sx, sy),
            (lx0, sy),
            (lx0, mid_y),
            (final_x, mid_y),
            (final_x, ty),
        )
        transition_leg = 3
    return _perp_entry_l_record(
        points,
        tuple(members),
        transition_leg,
        edge.line_id,
        fan_source_offsets,
        flanked_gap,
    )


def _perp_entry_turn_is_planned(
    edge: Edge, family_id: RouteFamilyId, ctx: _RoutingCtx
) -> bool:
    """Whether an exit-turn plan owns *edge*'s turn as *family_id*."""
    if ctx.exit_turns is None:
        return False
    membership = ctx.exit_turns.membership_for_edge(edge)
    return (
        membership is not None
        and membership.axis is not None
        and membership.assignment is not None
        and membership.assignment.planned_family_id is family_id
    )


def _route_top_entry_l_shape(
    edge: Edge,
    src: Station,
    tgt: Station,
    n: int,
    ctx: _RoutingCtx,
    channel_y: float | None = None,
) -> RoutedPath:
    """Staircase route into a TOP entry port, fanned along one centreline.

    The shape is :func:`_perp_entry_l_geometry`; a junction standing in the
    port's own column travels that column instead, with no fan.
    """
    geometry = _perp_entry_l_geometry(
        edge,
        src,
        tgt,
        n,
        ctx,
        PortSide.TOP,
        channel_y,
        planned=_perp_entry_turn_is_planned(edge, RouteFamilyId.TOP_ENTRY_L_SHAPE, ctx),
    )
    if geometry is None:
        straight_drop_route = _perp_entry_junction_straight_drop(edge, src, tgt, ctx)
        assert straight_drop_route is not None
        return straight_drop_route
    return _perp_entry_finish_route(edge, geometry, ctx)


def _bottom_entry_below_channel_y(ctx: _RoutingCtx, tgt_sec: Section) -> float:
    """Y of the routing channel just below a BOTTOM-entry target's box.

    A BOTTOM entry is reached from below, so the rise into the port departs
    from a channel that clears the target section's own bottom edge.  Unlike
    the TOP-entry channel there is no header badge to clear on this side, so
    the plain route-clearance offset is enough.
    """
    return header_corridor_y(
        ctx.graph,
        tgt_sec.grid_row,
        below=True,
        base_radius=ctx.curve_radius,
        default=tgt_sec.bbox_y + tgt_sec.bbox_h,
    )


def _bottom_entry_above_wrap_riser_x(
    src: Station,
    tgt: Station,
    final_x: float,
    below_y: float,
    ctx: _RoutingCtx,
) -> float | None:
    """Riser X to route an above-row feeder around a section into its BOTTOM port.

    Mirror of :func:`_top_entry_below_wrap_riser_x` for the opposite entry
    side: a BOTTOM entry port sits on the target's bottom edge, so it must be
    entered from below.  When the feeder's source section is in a higher grid
    row than the target the inter-row channel lies *above* the target, and a
    straight drop into the port would plough down through the box interior.
    The leg instead carries past one vertical side of the box, drops to the
    channel below it, then comes back under the bottom into the port.

    Returns the X of that riser -- outside the box on a side clear of every
    other section, preferring the side away from the feeder's approach.
    Returns ``None`` when the feeder is level with or below the target (the
    ordinary from-below approach), or when neither side is clear (the
    fall-through route then handles it and the runtime guard stays the
    backstop).
    """
    graph = ctx.graph
    src_sec = resolve_section(graph, src)
    tgt_sec = resolve_section(graph, tgt)
    if src_sec is None or tgt_sec is None or src_sec.grid_row >= tgt_sec.grid_row:
        return None

    exclude = {sid for sid in (src.section_id, tgt.section_id) if sid is not None}
    clearance = ctx.curve_radius + SECTION_ROUTE_CLEARANCE
    box_left = tgt_sec.bbox_x
    box_right = tgt_sec.bbox_x + tgt_sec.bbox_w
    right_x = col_right_edge(graph, tgt_sec.grid_col, default=box_right) + clearance
    left_x = col_left_edge(graph, tgt_sec.grid_col, default=box_left) - clearance
    # Prefer the side away from the feeder so the leg carries past the box and
    # returns under the bottom rather than re-crossing a near-side approach.
    prefer_right = src.x <= (box_left + box_right) / 2
    ordered = [right_x, left_x] if prefer_right else [left_x, right_x]

    def is_clear(rx: float) -> bool:
        return not (
            _h_segment_crosses_other_section(graph, src.x, rx, src.y, exclude)
            or _v_segment_crosses_other_section(graph, rx, src.y, below_y, exclude)
            or _h_segment_crosses_other_section(graph, rx, final_x, below_y, exclude)
        )

    return next((rx for rx in ordered if is_clear(rx)), None)


def _route_bottom_entry_l_shape(
    edge: Edge,
    src: Station,
    tgt: Station,
    n: int,
    ctx: _RoutingCtx,
    channel_y: float | None = None,
) -> RoutedPath:
    """Staircase route into a BOTTOM entry port, fanned along one centreline.

    Mirror of :func:`_route_top_entry_l_shape` for the opposite entry side: the
    trunk run in the inter-row gap sits below the target section and rises into
    the port.
    """
    geometry = _perp_entry_l_geometry(
        edge,
        src,
        tgt,
        n,
        ctx,
        PortSide.BOTTOM,
        channel_y,
        planned=_perp_entry_turn_is_planned(
            edge, RouteFamilyId.BOTTOM_ENTRY_L_SHAPE, ctx
        ),
    )
    if geometry is None:
        straight_drop_route = _perp_entry_junction_straight_drop(edge, src, tgt, ctx)
        assert straight_drop_route is not None
        return straight_drop_route
    return _perp_entry_finish_route(edge, geometry, ctx)


def _left_exit_left_entry_drop_channel_x(
    edge: Edge, src: Station, tgt: Station, ctx: _RoutingCtx
) -> float:
    """The descent column a LEFT-exit-to-LEFT-entry drop finally stands on.

    The column is derived from the leftmost of the two boxes' left edges, which
    over-states what the corridor crossing that margin actually charges, and
    :func:`~nf_metro.layout.routing.normalize._hold_runs_in_corridor_clearance`
    then travels the drop onto the band its own reservation realises.  Reading
    that travel here is what lets a plan naming this axis and the drawn descent
    stand on one column, the way
    :func:`seated_left_exit_under_target_descent` does for the far-side loop.
    """
    src_col = _resolve_section_col(ctx.graph, src)
    tgt_col = _resolve_section_col(ctx.graph, tgt)
    left_edge = min(
        col_left_edge(ctx.graph, src_col, default=src.x),
        col_left_edge(ctx.graph, tgt_col, default=tgt.x),
    )
    channel_x = min(left_edge, src.x, tgt.x) - ctx.curve_radius - ctx.offset_step
    members, _src_center, _tgt_center = gather_tapered_bundle(ctx, edge)
    sign = right_normal_axis_sign(Direction.D if tgt.y > src.y else Direction.U)
    return channel_x + seat_bundle_in_corridor_clearance(
        ctx.graph,
        axis=0,
        section_ids=section_ids_of_stations(ctx.graph, src, tgt),
        lanes=[channel_x + offset * sign for _e, _lid, _so, offset in members],
        run_start=min(src.y, tgt.y),
        run_end=max(src.y, tgt.y),
    )


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
    channel_x = _left_exit_left_entry_drop_channel_x(edge, src, tgt, ctx)

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

    The bump is spent leftward, which for a channel descending the map's left
    margin is spent toward the canvas edge.  It is therefore capped at the room
    the margin leaves outboard of ``canvas_edge_clearance()``: a stroke and its
    direction chevron drawn past that are clipped by the viewport, whereas
    ``SECTION_ROUTE_CLEARANCE`` is a legibility gap beside a box edge that a run
    can give up.
    """
    base_gap = ctx.curve_radius + ctx.offset_step
    max_delta = (n_outer - 1) * ctx.offset_step / 2
    extra_clearance = max(0.0, SECTION_ROUTE_CLEARANCE - (base_gap - max_delta))
    outboard_room = max(
        0.0, anchor_x - base_gap + signed_delta - canvas_edge_clearance()
    )
    return anchor_x - base_gap - min(extra_clearance, outboard_room) + signed_delta


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


def _fan_shares_inter_row_channel(ctx: _RoutingCtx, edge: Edge) -> bool:
    """True when *edge*'s fan-out junction sends two or more distinct lines into
    *edge*'s target row.

    Those branches drop into the same inter-row gap and co-travel it before
    peeling into their own targets, so they form one bundle that must keep an
    ``OFFSET_STEP`` between its lines.  A junction whose only branch into this
    row is *edge*'s own line runs a solo channel with nothing to separate from,
    so it keeps the plain band-centred run rather than a fan stagger.
    """
    graph = ctx.graph
    port = graph.ports.get(edge.target)
    tgt_sec = graph.sections.get(port.section_id) if port is not None else None
    if tgt_sec is None:
        return False
    lines: set[str] = set()
    for sibling in graph.edges_from(edge.source):
        sib_port = graph.ports.get(sibling.target)
        sib_sec = graph.sections.get(sib_port.section_id) if sib_port else None
        if sib_sec is not None and sib_sec.grid_row == tgt_sec.grid_row:
            lines.add(sibling.line_id)
    return len(lines) >= 2


def _fan_corner_run(ctx: _RoutingCtx, pos_n: int) -> float:
    """Run from a fan's source to the centreline of its first corner column.

    A fan lays its centreline down the middle of the bundle, so its lanes sit
    half a bundle width either side.  Standing the centreline off by the base
    radius plus that half width puts the nearest lane a full base radius clear of
    the source, which is what every lane's arc is anchored on.
    """
    return ctx.curve_radius + bundle_width(pos_n, ctx.offset_step) / 2


def _fan_stand_off_x(
    ctx: _RoutingCtx, src: Station, pos_n: int, horizontal: Direction
) -> float:
    """Nearest column a bundle of *pos_n* lines may turn in leaving *src*.

    The base radius plus half the bundle width puts the nearest lane's arc a
    full radius clear of the source; a source sitting on its section's own edge
    is stood off further so the channel does not read as flush against the box.
    """
    sign = 1.0 if horizontal is Direction.R else -1.0
    section = ctx.graph.sections.get(src.section_id) if src.section_id else None
    if section is not None and section.bbox_w > 0:
        near_edge = section.bbox_x + section.bbox_w if sign > 0 else section.bbox_x
    else:
        near_edge = src.x
    edge_gap = sign * (src.x + sign * ctx.curve_radius - near_edge)
    return (
        src.x
        + sign * _fan_corner_run(ctx, pos_n)
        + sign * max(0.0, SECTION_ROUTE_CLEARANCE - edge_gap)
    )


def _fan_corner_x(
    ctx: _RoutingCtx,
    src: Station,
    pos_n: int,
    horizontal: Direction,
    *,
    facts: _InterFacts | None = None,
) -> float:
    """The column every branch of one junction fan turns through.

    Each branch's channel is a signed rank offset from this single column
    (:func:`l_shape_stagger`), so the descent-X order across the fan is in phase
    with the lead-in Y order only while every branch resolves the column
    identically -- and one fan's branches are claimed by different handler
    families (U-bypass, entry wrap), which is why the column is resolved here
    rather than per family.  Centred on the inter-column gap the branches
    descend in, but never nearer the source than its own stand-off.
    """
    if facts is not None:
        ctx, src = facts.ctx, facts.src
    stand_off = _fan_stand_off_x(ctx, src, pos_n, horizontal)
    src_col, src_row = (
        facts.section_colrow(src)
        if facts is not None
        else _resolve_section_colrow(ctx.graph, src)
    )
    if src_col is None:
        return stand_off
    rightward = horizontal is Direction.R
    src_sec = resolve_section(ctx.graph, src, prefer_upstream=False)
    slot = _gap_channel_base(
        ctx.graph,
        src_col if rightward else src_col - 1,
        src_row,
        pos_n,
        ctx.offset_step,
        anchor_section_id=src_sec.id if src_sec is not None else None,
        anchor_side=PortSide.RIGHT if rightward else PortSide.LEFT,
    )
    return max(stand_off, slot) if rightward else min(stand_off, slot)


def _wrap_fan_geometry(
    ctx: _RoutingCtx,
    edge: Edge,
    src: Station,
    i: int,
    n: int,
    vertical: Direction,
    *,
    include_fan: bool = True,
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
    fan = (
        ctx.junction_fan_info.get((edge.source, edge.target, edge.line_id))
        if include_fan
        else None
    )
    pos_i, pos_n = fan if fan is not None else (i, n)
    delta = l_shape_stagger(pos_i, pos_n, vertical, ctx.offset_step)
    resolve = _fan_corner_x if fan is not None else _fan_stand_off_x
    corner_x = resolve(ctx, src, pos_n, Direction.R)
    return fan, pos_n, delta, corner_x


def _entry_wrap_run_displacement(
    delta: float, corner_x: float, descent_x: float
) -> float:
    """Signed Y a member of :func:`_route_entry_wrap` sits off its centreline
    along the horizontal channel run.

    The loop's member carries ``-delta`` on the bundle's right-hand normal, so
    which side of the centreline it draws on depends on which way the traverse
    travels.
    """
    run = Direction.R if descent_x >= corner_x else Direction.L
    return -delta * right_normal_axis_sign(run)


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


@dataclass(frozen=True, slots=True)
class _EntryWrapGeometry:
    pos_n: int
    delta: float
    corner_x: float
    channel_y: float
    descent_x: float
    seam: _SourceSeam


def _entry_wrap_record(
    ctx: _RoutingCtx,
    edge: Edge,
    src: Station,
    *,
    pos_n: int,
    delta: float,
    corner_x: float,
    channel_y: float,
    descent_x: float,
) -> _EntryWrapGeometry:
    """Complete an entry-wrap record from the channels its leaf resolved.

    The loop leaves the source horizontally at the line's own station lateral
    and turns into the corner column, so ``launch_coordinate`` is the source X.
    ``corner_x`` places the bundle's centreline; the member draws its turn one
    stagger off that on the turn leg's normal, which is the column
    ``axis_coordinate`` names.
    """
    from nf_metro.layout.routing.exit_turns import ExitTurnInvariantError

    src_off = _get_offset(ctx, edge.source, edge.line_id)
    try:
        turn_direction = _leg_direction(
            (src.x, src.y + src_off + delta), (src.x, channel_y)
        )
    except ExitTurnInvariantError as e:
        raise ExitTurnInvariantError(
            f"{e} [entry-wrap drop for source {edge.source!r} line {edge.line_id!r}]"
        ) from e
    run_direction = segment_direction((src.x, src.y), (corner_x, src.y))
    if run_direction is None:
        # The corner sits on the source's own column, so there is no lead-out
        # run before the turn: the branch drops straight down the junction
        # column.  A centreline that never turns states only the drop it opens
        # on (the coincident-column bottom-exit seam does the same).
        seam = _SourceSeam(turn_direction, None, None, None)
    else:
        seam = _SourceSeam(
            run_direction,
            turn_direction,
            src.x,
            corner_x - delta * right_normal_axis_sign(turn_direction),
        )
    return _EntryWrapGeometry(pos_n, delta, corner_x, channel_y, descent_x, seam)


def _left_entry_wrap_geometry(
    ctx: _RoutingCtx, edge: Edge, src: Station, tgt: Station, i: int, n: int
) -> _EntryWrapGeometry:
    """Resolve the seam shared by left-entry-wrap planning and emission.

    See :func:`_route_left_entry_wrap` for the shape this describes.
    """
    sy, ty = src.y, tgt.y
    dy = ty - sy
    # Lead-out and LEFT-entry lead-in both run rightward, so port-offset stacking
    # fixes the concentric order regardless of riser direction; force the DOWN
    # (rightward-run) stagger so the body nests into both baked endpoints. ``dy``
    # only picks the channel Y below.
    fan, pos_n, delta, corner_x = _wrap_fan_geometry(ctx, edge, src, i, n, Direction.D)

    # Horizontal channel Y in the inter-row gap.  A fanned wrap traverses the
    # junction's shared corridor band so it coincides with the fan's other
    # inter-row-gap branches; an un-fanned wrap centres its own run via
    # ``inter_row_channel_y``, which clamps the per-line stagger inside the
    # clearance band (a narrow gap must not let the run graze the source box) --
    # the builder re-adds ``delta`` on the leftward traverse, so pre-subtract it
    # here to land on the clamped Y.
    corridor = ctx.fan_corridors.get(edge.source)
    if fan is not None and corridor is not None and corridor.band_y is not None:
        hy = corridor.band_y
    elif fan is not None and _fan_shares_inter_row_channel(ctx, edge):
        # A fanned wrap with no shared corridor band but sibling branches
        # co-travelling this inter-row gap shares the channel with them.  Seat it
        # on the same band centre the top-entry siblings use -- the unclamped gap
        # centre, lifted so the fan bundle clears the source section's bottom
        # edge (mirroring the top-entry's n>1 lift).  The builder re-adds
        # ``delta`` on the traverse, giving the concentric ``band + delta``
        # stagger the top-entry taper produces, so the two run one OFFSET_STEP
        # apart rather than a frame offset apart.  Clamping into the (often
        # degenerate) band would instead collapse the shared channel.
        hy = inter_row_channel_y(
            ctx.graph,
            src,
            tgt,
            sy,
            ty,
            dy,
            ctx.curve_radius,
            reserved=ctx.reserved_bands.rows,
        )
        src_sec = resolve_section(ctx.graph, src)
        if src_sec is not None:
            src_bottom = src_sec.bbox_y + src_sec.bbox_h
            hy = held_in_reserved_band(
                max(
                    hy,
                    src_bottom
                    + INTER_ROW_EDGE_CLEARANCE
                    + (pos_n - 1) * ctx.offset_step,
                ),
                ctx.reserved_bands.rows.at(src_sec.grid_row + 1),
            )
    else:
        channel_y = inter_row_channel_y(
            ctx.graph,
            src,
            tgt,
            sy,
            ty,
            dy,
            ctx.curve_radius,
            delta,
            reserved=ctx.reserved_bands.rows,
        )
        claimed_band = ctx.reserved_bands.claimed_row_band(
            edge.source, edge.target, edge.line_id
        )
        if claimed_band is not None:
            channel_y = claimed_band.hold(channel_y)
        hy = channel_y - delta

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

    return _entry_wrap_record(
        ctx,
        edge,
        src,
        pos_n=pos_n,
        delta=delta,
        corner_x=corner_x,
        channel_y=hy,
        descent_x=vx,
    )


def _emit_left_entry_wrap(
    edge: Edge,
    src: Station,
    entry: Station,
    ctx: _RoutingCtx,
    geometry: _EntryWrapGeometry,
) -> RoutedPath:
    """Draw one resolved LEFT-entry wrap loop and declare its two channels."""
    route = _route_entry_wrap(
        edge,
        src,
        entry,
        ctx,
        pos_n=geometry.pos_n,
        delta=geometry.delta,
        corner_x=geometry.corner_x,
        channel_y=geometry.channel_y,
        descent_x=geometry.descent_x,
        entry_side=PortSide.LEFT,
    )
    _declare_channel(
        route,
        ctx,
        geometry.descent_x,
        vertical_direction(entry.y - geometry.channel_y),
    )
    _declare_channel(
        route,
        ctx,
        geometry.corner_x,
        vertical_direction(geometry.channel_y - src.y),
    )
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
    geometry = _left_entry_wrap_geometry(ctx, edge, src, tgt, i, n)
    return _emit_left_entry_wrap(edge, src, tgt, ctx, geometry)


@dataclass(frozen=True, slots=True)
class _PerpExitFarSideWrapLoop:
    """The loop a trailing perp exit draws to a far-side side entry.

    ``seam`` is the drop down the exit column and the inter-row channel it
    turns onto.
    """

    entry_side: PortSide
    pos_n: int
    delta: float
    corner_x: float
    channel_y: float
    descent_x: float
    seam: _SourceSeam


def perp_exit_farside_entry_wrap_geometry(f: _InterFacts) -> _PerpExitFarSideWrapLoop:
    """Resolve the loop shared by far-side perp-exit planning and emission."""
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
    hy = inter_row_channel_y(
        ctx.graph,
        src,
        tgt,
        sy,
        ty,
        dy,
        ctx.curve_radius,
        delta,
        reserved=ctx.reserved_bands.rows,
    )
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
    return _PerpExitFarSideWrapLoop(
        entry_side,
        pos_n,
        delta,
        corner_x,
        hy,
        vx,
        _SourceSeam(
            Direction.D if dy > 0 else Direction.U,
            Direction.R if vx > corner_x else Direction.L,
            sy,
            hy,
        ),
    )


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
    geometry = perp_exit_farside_entry_wrap_geometry(f)
    route = _route_entry_wrap(
        edge,
        src,
        tgt,
        ctx,
        pos_n=geometry.pos_n,
        delta=geometry.delta,
        corner_x=geometry.corner_x,
        channel_y=geometry.channel_y,
        descent_x=geometry.descent_x,
        entry_side=geometry.entry_side,
        source_leads_down=True,
    )
    _declare_channel(
        route,
        ctx,
        geometry.descent_x,
        Direction.D if tgt.y > geometry.channel_y else Direction.U,
    )
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
    return centre_inter_column_channel(
        ctx.graph,
        ep_col - 1,
        ep_col,
        row=ep_row,
        offset=delta,
        reserved=ctx.reserved_bands.columns,
    )


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
        tgt = graph.station_for_edge_target(edge)
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
      below the row and use their dedicated handler);
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
    corridor = ctx.fan_corridors.get(edge.source)
    if fan is not None and corridor is not None and corridor.band_y is not None:
        # Traverse the fan's one shared band so this feeder's H leg coincides
        # with the sibling wrap's rather than smearing a few px apart.
        gy_base = corridor.band_y
    elif fan is not None:
        # Fan whose junction earned no corridor (its in-column gap below does not
        # fit the bundle): centre on the global row edges to coincide with the
        # sibling wrap, which centres the same way.
        wrap_top = row_bottom_edge(ctx.graph, src_row, default=gap_top)
        wrap_bottom = row_top_edge(ctx.graph, src_row + 1, default=wrap_top)
        gy_base = _center_inter_row_channel(
            wrap_top, wrap_bottom, reserved=ctx.reserved_bands.rows.at(src_row + 1)
        )
    elif gap_bottom > gap_top:
        gy_base = _center_inter_row_channel(
            gap_top, gap_bottom, reserved=ctx.reserved_bands.rows.at(src_row + 1)
        )
    else:
        gy_base = gap_top + INTER_ROW_EDGE_CLEARANCE
    # Keep the channel inside the clearance band: at least
    # INTER_ROW_EDGE_CLEARANCE below the source-row bottom and clear of the
    # next row's header badge.  Skipped for fan feeders, which share the wrap
    # sibling's (unclamped) band so the two bundles' H legs coincide.
    if fan is None and gap_bottom > gap_top:
        gy_base = held_in_reserved_band(
            min(
                max(gy_base, gap_top + INTER_ROW_EDGE_CLEARANCE),
                gap_bottom - INTER_ROW_HEADER_CLEARANCE,
            ),
            ctx.reserved_bands.rows.at(src_row + 1),
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


def _around_section_below_geometry(
    ctx: _RoutingCtx,
    edge: Edge,
    src: Station,
    entry_port: Station,
    i: int,
    n: int,
    channel_y: float | None = None,
    *,
    include_fan: bool = True,
) -> _EntryWrapGeometry:
    """Resolve the seam shared by around-below planning and emission.

    See :func:`_route_around_section_below` for the shape this describes and
    for what *channel_y* overrides.
    """
    sy = src.y
    ex, ey = entry_port.x, entry_port.y

    # The route shares its first corner with any sibling routes from the same
    # junction (junction_fan_info pivots all outgoing edges through one shared
    # corner; merge-branch edges are excluded, so for the merge case the fan is
    # typically absent and the edge's own bundle position is used).
    _fan, pos_n, delta, corner_x = _wrap_fan_geometry(
        ctx,
        edge,
        src,
        i,
        n,
        vertical_direction(ey - sy),
        include_fan=include_fan,
    )

    # Bypass Y below all sections in the column range so the route
    # clears every intervening section (cross_row=True).
    src_col, src_row = _resolve_section_colrow(ctx.graph, src)
    ep_col = _resolve_section_col(ctx.graph, entry_port)
    # Fallbacks if a column can't be resolved (degenerate cases).
    bc_src_col = src_col if src_col is not None else 0
    bc_tgt_col = ep_col if ep_col is not None else bc_src_col
    if channel_y is not None:
        by = channel_y
    else:
        # The bypass bottom is the clearance the lane nearest the boxes above it
        # owes them, and the bundle stacks from its centreline toward that edge,
        # so the whole ladder seats one half-width deeper.  A run settled after
        # the fact would be pushed here anyway; a planned turn leg is frozen
        # against that settlement and so has to state it.
        by = (
            bypass_bottom_y(
                ctx.graph,
                bc_src_col,
                bc_tgt_col,
                BYPASS_CLEARANCE,
                src_row=src_row,
                cross_row=True,
            )
            + bundle_width(pos_n, ctx.offset_step) / 2
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

    if channel_y is not None:
        # ``by`` holds the level the branch feeders drop onto and the gap was
        # reserved to hold, but ``_route_entry_wrap`` reads its channel Y as the
        # bundle centreline and draws this member ``delta`` off it.  Seat the
        # centreline the other way so the trunk's own track lands on that level.
        by -= _entry_wrap_run_displacement(delta, corner_x, vx)

    return _entry_wrap_record(
        ctx,
        edge,
        src,
        pos_n=pos_n,
        delta=delta,
        corner_x=corner_x,
        channel_y=by,
        descent_x=vx,
    )


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
    del tgt
    geometry = _around_section_below_geometry(
        ctx, edge, src, entry_port, i, n, channel_y
    )
    return _emit_left_entry_wrap(edge, src, entry_port, ctx, geometry)


def _wrap_lane_coordinates(
    members: Sequence[_Member], centre: float, run: Direction
) -> list[tuple[tuple[str, str, str], float]]:
    """Each bundle lane's own coordinate on a leg travelling *run*."""
    sign = right_normal_axis_sign(run)
    return [
        ((member.source, member.target, line_id), centre + offset * sign)
        for member, line_id, offset in members
    ]


@dataclass(frozen=True, slots=True)
class _OverTopGeometry:
    lead_x: float
    channel_y: float
    descent_x: float
    source_y: float
    entry_y: float
    seam: _SourceSeam


def _right_entry_over_top_geometry(
    ctx: _RoutingCtx, edge: Edge, src: Station, tgt: Station
) -> _OverTopGeometry:
    """Resolve the seam shared by over-top planning and emission.

    See :func:`_route_right_entry_over_top` for the loop this describes.  The
    bundle keeps its source mean end to end, so both the source-side and the
    entry-side horizontals sit on ``src_center``; this line's own rank against
    that centreline is what puts its rise one stagger off ``lead_x``.
    """
    graph = ctx.graph
    members, src_center, _tgt_center = gather_bundle(ctx, edge)

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

    # Keep the over-top channel below the bottom of any upstream section its
    # horizontal span crosses: channel_y derives from the target's own top and
    # would otherwise sit inside a taller row-mate above the span whose bottom
    # edge dips into the band.
    exclude = frozenset(
        sid for sid in (src.section_id, tgt.section_id) if sid is not None
    )
    crossed_bottom = lowest_section_bottom_crossing_span(
        graph,
        min(lead_x, descent_x),
        max(lead_x, descent_x),
        above_y=section_top,
        exclude=exclude,
    )
    if crossed_bottom is not None:
        fan_reach = (len(members) - 1) * ctx.offset_step
        channel_y = max(
            channel_y, crossed_bottom + INTER_ROW_EDGE_CLEARANCE + fan_reach
        )

    mid_sy = sy + src_center
    offset = next(off for _member, line_id, off in members if line_id == edge.line_id)
    turn_direction = _leg_direction((lead_x, mid_sy), (lead_x, channel_y))
    # Both channels are placed from the grid edges to hand, which under-states
    # what their own corridors' reservations charge them; travel each bundle onto
    # the band its claims realise so the loop stands where the pre-freeze seating
    # would put it rather than being moved off an axis the plan has stated.
    # The loop's own descent shares that corridor, and the two verticals are
    # joined by the traverse whose ends they turn on, so the rise may only take
    # the travel that leaves both corners their full radius on the side it
    # already stands: a corridor and a descent that share no such coordinate
    # leave the rise where it stands.
    runway = 2 * ctx.curve_radius
    clear_of_descent = (
        ReservedBand(descent_x + runway, inf)
        if lead_x > descent_x
        else ReservedBand(-inf, descent_x - runway)
    )
    lead_x += seat_bundle_in_corridor_clearance(
        graph,
        axis=0,
        section_ids=section_ids_of_stations(graph, src, tgt),
        lanes=[
            lane
            for _key, lane in _wrap_lane_coordinates(members, lead_x, turn_direction)
        ],
        run_start=min(mid_sy, channel_y),
        run_end=max(mid_sy, channel_y),
        clear_of=clear_of_descent,
    )
    lead_x += seat_bundle_in_claimed_bands(
        ctx.reserved_bands,
        _wrap_lane_coordinates(members, lead_x, turn_direction),
        rank=1,
    )
    channel_y += seat_bundle_in_claimed_bands(
        ctx.reserved_bands,
        _wrap_lane_coordinates(members, channel_y, Direction.R),
        rank=2,
    )
    return _OverTopGeometry(
        lead_x,
        channel_y,
        descent_x,
        mid_sy,
        ey + src_center,
        _SourceSeam(
            _leg_direction((sx, mid_sy), (lead_x, mid_sy)),
            turn_direction,
            sx,
            lead_x + offset * right_normal_axis_sign(turn_direction),
        ),
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
    internal order (driven from the arrival bundle by
    :func:`offsets._reorder_reconvergence`).

    Built via :func:`build_concentric_bundle` from the bundle's centreline, so
    the loop cannot flip and every corner is concentric by construction.
    """
    # A U-turn keeps the bundle centred on its source mean end-to-end (the
    # descent's reversal is matched by the section's reversed internal order).
    members, _src_center, _tgt_center = gather_bundle(ctx, edge)
    geometry = _right_entry_over_top_geometry(ctx, edge, src, tgt)
    centerline = [
        (src.x, geometry.source_y),
        (geometry.lead_x, geometry.source_y),
        (geometry.lead_x, geometry.channel_y),
        (geometry.descent_x, geometry.channel_y),
        (geometry.descent_x, geometry.entry_y),
        (tgt.x, geometry.entry_y),
    ]
    route = route_along(edge, members, centerline, base_radius=ctx.curve_radius)
    _declare_channel(
        route,
        ctx,
        geometry.descent_x,
        vertical_direction(geometry.entry_y - geometry.channel_y),
    )
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

    The wrap answers this from published channel claims alone. Its turn axis is
    a coordinate its own plan states, and a fact read off the routes built so
    far would make that coordinate depend on emission order. The claims are
    selected by canonical edge rank, so they say the same thing in either pass.
    """
    lo, hi = (y_lo, y_hi) if y_lo <= y_hi else (y_hi, y_lo)
    if ctx.convergences is None:
        return False
    for claim in ctx.convergences.prior_channel_claims_for_edge(edge):
        if claim.line_id != edge.line_id or claim.owner_source == edge.source:
            continue
        if not (corner_x - COORD_TOLERANCE <= claim.x <= gap_right + COORD_TOLERANCE):
            continue
        if min(hi, claim.y_hi) - max(lo, claim.y_lo) > COORD_TOLERANCE:
            return True
    return False


@dataclass(frozen=True, slots=True)
class _RightEntryWrapGeometry:
    """A cross-row RIGHT-entry wrap's channels, and which shape draws them.

    ``drop_in`` names the straight descent down the corridor right of the target
    column, which the wrap collapses to when that corridor is clear; the staged
    wrap through the inter-row channel is the general case.  Both open on the
    same lead-out and differ only in the Y their source-side descent runs to,
    which is what ``wrap`` carries.
    """

    wrap: _EntryWrapGeometry
    drop_in: bool


def _right_entry_wrap_geometry(f: _InterFacts) -> _RightEntryWrapGeometry:
    """Resolve the seam shared by cross-row RIGHT-entry-wrap planning and emission.

    See :func:`_route_right_entry_wrap` for the shapes this describes.  Only
    valid for the cross-row case -- a same-row source loops over the top instead
    (:func:`_right_entry_over_top_geometry`).
    """
    ctx, edge, src, tgt = f.ctx, f.edge, f.src, f.tgt
    src_col, src_row, tgt_col, tgt_row = f.src_col, f.src_row, f.tgt_col, f.tgt_row
    assert src_col is not None and tgt_col is not None and tgt_row is not None
    vertical = vertical_direction(f.dy)

    _fan, pos_n, delta, corner_x = _wrap_fan_geometry(
        ctx, edge, src, f.i, f.n, vertical
    )

    # V2 descent channel centre, just past the entry port in the gap to the
    # right of the target column.
    vx = _right_entry_descent_x(ctx, f.tx, pos_n)

    # Horizontal channel Y centre, below the source row's sections, seated in
    # the clearance its own corridor owes over the stretch it traverses.
    hy = seat_run_in_corridor_clearance(
        ctx.graph,
        axis=1,
        section_ids=section_ids_of_stations(ctx.graph, src, tgt),
        coordinate=bypass_bottom_y(
            ctx.graph, src_col, tgt_col, BYPASS_CLEARANCE, src_row=src_row
        ),
        run_start=min(corner_x, vx),
        run_end=max(corner_x, vx),
    )
    # That seat measures the corridor over the run's own endpoint sections,
    # which under-states a boundary the reservation charges over a wider span;
    # where the claim has realised its band, it is the one the closing guard
    # reads, and a frozen traverse has no later pass to move it back inside.
    hy += seat_bundle_in_claimed_bands(
        ctx.reserved_bands,
        [
            (
                (edge.source, edge.target, edge.line_id),
                hy + _entry_wrap_run_displacement(delta, corner_x, vx),
            )
        ],
        rank=2,
    )

    # A same-line descent from another source already in the lead-out gap would
    # merge with a source-hugging turn-down into one corner.  Carry the
    # horizontal on and turn down clear to its right (bounded at the target row
    # so the drop misses the descent but never reaches a right-column section).
    _gap_left, gap_right = column_gap_edges(
        ctx.graph, src_col, src_col + 1, row=tgt_row
    )
    if _leadout_self_meets_sibling_descent(ctx, edge, corner_x, f.sy, hy, gap_right):
        corner_x = max(corner_x, gap_right - ctx.curve_radius - ctx.offset_step)

    # Same-column source (stacked directly above) drops straight down the
    # corridor when clear, leading to it at the top corner rather than down the
    # wrap's inter-row staging channel.  An adjacent-column source keeps the wrap
    # so its band traverse runs through the inter-row channel between the boxes.
    drop_in = src_col == tgt_col and _right_entry_corridor_drop_in_is_clear(
        ctx.graph, src, tgt, vx
    )
    if drop_in:
        corner_x = vx
        # The descent runs straight to the entry lateral, so that -- not the
        # staging channel -- is the Y the opening turn heads for.
        hy = tgt.y + _get_offset(ctx, edge.target, edge.line_id) - delta
    return _RightEntryWrapGeometry(
        _entry_wrap_record(
            ctx,
            edge,
            src,
            pos_n=pos_n,
            delta=delta,
            corner_x=corner_x,
            channel_y=hy,
            descent_x=vx,
        ),
        drop_in,
    )


def _route_right_entry_wrap(f: _InterFacts) -> RoutedPath:
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
    edge, src, tgt, ctx = f.edge, f.src, f.tgt, f.ctx
    # The cross-row channel is a bypass-style Y just below the source row's
    # sections, so the line stays high and only drops at the target column.
    if not (f.cross_row and f.src_col is not None and f.tgt_col is not None):
        # Same-row source: loop over the top into the right port (the channel
        # below would cut the interior).  Built as a concentric bundle.
        over_top = _route_right_entry_over_top(edge, src, tgt, ctx)
        assert over_top is not None  # edge is always among its own bundle members
        return over_top

    geometry = _right_entry_wrap_geometry(f)
    seam = geometry.wrap
    if geometry.drop_in:
        return _route_right_entry_drop_in(
            edge,
            src,
            tgt,
            ctx,
            pos_n=seam.pos_n,
            delta=seam.delta,
            corner_x=seam.descent_x,
        )

    tgt_col, tgt_row = f.tgt_col, f.tgt_row
    assert tgt_col is not None
    route = _route_entry_wrap(
        edge,
        src,
        tgt,
        ctx,
        pos_n=seam.pos_n,
        delta=seam.delta,
        corner_x=seam.corner_x,
        channel_y=seam.channel_y,
        descent_x=seam.descent_x,
        entry_side=PortSide.RIGHT,
    )
    route.declare_gap_slot(
        lo_col=tgt_col,
        hi_col=tgt_col + 1,
        row=tgt_row,
        direction=vertical_direction(f.ty - seam.channel_y),
        slot_index=0,
        n_slots=1,
    )
    return route


def _gap_above_target_y(graph: MetroGraph, tgt_row: int) -> tuple[float, float]:
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


def _right_entry_gap_above_is_clear(f: _InterFacts) -> bool:
    """Whether a RIGHT-entry feed from above can use the inter-row gap.

    The route runs its long horizontal in the band just above the target
    row, then drops straight down the RIGHT side of the target column into
    the port.  Viable only when that band genuinely exists (the row above the
    target's bottom is above the target row's top), is wide enough for the
    traverse to clear both the upper row's bottom edge and the target row's
    header badge, and the horizontal at the band's centre crosses no section
    interior between the source and the target's right edge.
    """
    graph, src, entry_port, tgt_row = f.graph, f.src, f.tgt, f.tgt_row
    assert tgt_row is not None
    gap_top, gap_bottom = _gap_above_target_y(graph, tgt_row)
    if gap_bottom <= gap_top:
        return False
    # A band too narrow for both clearances makes the centred run graze the
    # source box bottom, so the feed loops around below the target row instead.
    if not _inter_row_band_fits(gap_top, gap_bottom):
        return False
    gy = _center_inter_row_channel(
        gap_top, gap_bottom, reserved=f.ctx.reserved_bands.rows.at(tgt_row)
    )

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
    return not f.h_segment_crosses_other_section(src.x, section_right, gy)


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
    gap_top, gap_bottom = _gap_above_target_y(ctx.graph, tgt_row)
    channel_y_base = _center_inter_row_channel(
        gap_top, gap_bottom, reserved=ctx.reserved_bands.rows.at(tgt_row)
    )
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


def _left_entry_gap_above_is_clear(f: _InterFacts) -> bool:
    """Whether a LEFT-entry feed from above can use the inter-row gap.

    The mirror of :func:`_right_entry_gap_above_is_clear`.  The route leads out
    right of the source, drops into the band just above the target row, runs
    LEFTWARD along it, then drops down the LEFT (outward) side of the target
    column into the port.  Viable only when that band genuinely exists, is wide
    enough to clear both the upper row's bottom edge and the target row's header
    badge, and none of the three moving legs crosses another section interior:
    the source-side descent (a source stacked above a *wider* neighbour would
    drop into it), the band traverse, and the target-side descent.
    """
    graph, src, tgt, ctx = f.graph, f.src, f.tgt, f.ctx
    assert f.tgt_row is not None
    gap_top, gap_bottom = _gap_above_target_y(graph, f.tgt_row)
    if gap_bottom <= gap_top:
        return False
    if not _inter_row_band_fits(gap_top, gap_bottom):
        return False
    gy = _center_inter_row_channel(
        gap_top, gap_bottom, reserved=ctx.reserved_bands.rows.at(f.tgt_row)
    )
    _fan, pos_n, _delta, corner_x = _wrap_fan_geometry(
        ctx, f.edge, src, f.i, f.n, Direction.D
    )
    vx = _left_entry_descent_x(ctx, tgt.x, pos_n)
    if f.v_segment_crosses_other_section(corner_x, src.y, gy):
        return False
    if f.v_segment_crosses_other_section(vx, gy, tgt.y):
        return False
    return not f.h_segment_crosses_other_section(vx, src.x, gy)


def _left_entry_gap_above_geometry(
    ctx: _RoutingCtx,
    edge: Edge,
    src: Station,
    tgt: Station,
    i: int,
    n: int,
    tgt_row: int,
) -> _EntryWrapGeometry:
    """Resolve the seam shared by gap-above planning and emission.

    See :func:`_route_left_entry_via_gap_above` for the shape this describes.
    """
    gap_top, gap_bottom = _gap_above_target_y(ctx.graph, tgt_row)
    channel_y = _center_inter_row_channel(
        gap_top, gap_bottom, reserved=ctx.reserved_bands.rows.at(tgt_row)
    )
    _fan, pos_n, delta, corner_x = _wrap_fan_geometry(ctx, edge, src, i, n, Direction.D)
    return _entry_wrap_record(
        ctx,
        edge,
        src,
        pos_n=pos_n,
        delta=delta,
        corner_x=corner_x,
        channel_y=channel_y,
        descent_x=_left_entry_descent_x(ctx, tgt.x, pos_n),
    )


def _route_left_entry_via_gap_above(
    edge: Edge,
    src: Station,
    tgt: Station,
    i: int,
    n: int,
    ctx: _RoutingCtx,
    tgt_row: int,
) -> RoutedPath:
    """Route to a LEFT entry port via the inter-row gap ABOVE the target row.

    The mirror of :func:`_route_right_entry_via_gap_above`.  Used when the
    source sits in a row ABOVE the target and an intervening row blocks the
    source-adjacent wrap band: going AROUND BELOW the whole stack
    (:func:`_route_around_section_below`) dives to the canvas bottom and runs
    the full width back.  Instead run the long horizontal LEFTWARD in the clear
    band abutting the target row, then drop down the LEFT side of the target
    column into the LEFT entry port from its own outward side::

        (sx, sy)        -> H lead-in right out of the source
        (corner_x, sy)  ; turn down
        (corner_x, gy)  -> V down into the inter-row gap above the target
        (vx, gy)        -> H left past the target's left edge
        (vx, ey)        -> V down to the entry Y
        (ex, ey)        -> H right into the LEFT entry port

    The R-D-L-D-R loop is the same shape as :func:`_route_left_entry_wrap`; only
    the horizontal channel Y differs (the band above the target row rather than
    the band below the source).  The horizontal never crosses a section interior
    (guaranteed by :func:`_left_entry_gap_above_is_clear` at the call site).
    """
    geometry = _left_entry_gap_above_geometry(ctx, edge, src, tgt, i, n, tgt_row)
    return _emit_left_entry_wrap(edge, src, tgt, ctx, geometry)


def _source_is_boxed_fanout_junction(f: _InterFacts) -> bool:
    """Whether the edge's source is a fan-out junction boxed by a packed cell-mate.

    The band-hop only rescues a straddling fan-out whose exit-side cell-mate
    traps it; a plain fan-out (no pack) reaches its clear column via the
    gap-above path or the around-below dive.
    """
    graph = f.graph
    is_divergence = f.edge.source in f.ctx.fanout_junctions
    if not is_divergence:
        return False
    src_section = resolve_section(graph, f.src)
    return src_section is not None and graph.is_packed_section(src_section.id)


def _exit_side_pack_gap_midpoint(f: _InterFacts) -> float | None:
    """Midpoint of the gap between the source section and its exit-side cell-mate.

    The band-hop leads horizontally out of the junction into this gap before
    turning down, so the departure curves in the clear inter-cell gap rather
    than kinking at a right angle against the source section's edge.  Returns
    ``None`` when the source section or its exit-side cell-mate is unresolvable.
    """
    graph, src = f.graph, f.src
    source = resolve_section(graph, src)
    if source is None or source.bbox_w <= 0:
        return None
    source_right = source.bbox_x + source.bbox_w
    cellmate_left: float | None = None
    for members in graph.cell_packs.values():
        if source.id not in members:
            continue
        for other_id in members:
            other = graph.sections.get(other_id)
            if other is None or other.bbox_w <= 0 or other.id == source.id:
                continue
            if other.bbox_x <= source_right + COORD_TOLERANCE:
                continue  # cell-mate is not on the exit (right) side
            if cellmate_left is None or other.bbox_x < cellmate_left:
                cellmate_left = other.bbox_x
    if cellmate_left is None:
        return None
    return (source_right + cellmate_left) / 2


class _BandHopGeometry(NamedTuple):
    """A LEFT-entry band-hop's two channel Ys, three column Xs, and fan stagger.

    ``band0_y``/``band1_y`` are the bands just below the source row and just
    above the target row; ``lead_x`` is where the branch turns down into band0
    after leading out of the junction; ``corner_x`` is the descent column clear
    through the rows between the bands; ``vx`` is the target-side descent X;
    ``pos_n``/``delta`` are the source fan's bundle size and this line's offset.
    """

    band0_y: float
    band1_y: float
    lead_x: float
    corner_x: float
    vx: float
    pos_n: int
    delta: float


def _band_hop_geometry(f: _InterFacts) -> _BandHopGeometry | None:
    """Resolve a LEFT-entry band-hop's channels, columns, and fan.

    Returns ``None`` when either band is missing/too narrow, the two bands are
    not separated by an intervening row, or no clear descent column exists left
    of the source within the trap.
    """
    graph, src, tgt, ctx = f.graph, f.src, f.tgt, f.ctx
    assert f.src_row is not None and f.tgt_row is not None
    band0_top, band0_bottom = _gap_above_target_y(graph, f.src_row + 1)
    band1_top, band1_bottom = _gap_above_target_y(graph, f.tgt_row)
    # Both bands must clear the edge above and the next row's header badge below:
    # the band0 traverse runs above the intervening row, whose header can protrude
    # up into the gap, and band1 is where the branch descends past the target.
    if band0_bottom <= band0_top or not _inter_row_band_fits(band0_top, band0_bottom):
        return None
    if band1_bottom <= band1_top or not _inter_row_band_fits(band1_top, band1_bottom):
        return None
    if band1_top <= band0_bottom + COORD_TOLERANCE:
        return None  # the hop needs a row between the two bands
    band0_y = _center_inter_row_channel(
        band0_top, band0_bottom, reserved=ctx.reserved_bands.rows.at(f.src_row + 1)
    )
    band1_y = _center_inter_row_channel(
        band1_top, band1_bottom, reserved=ctx.reserved_bands.rows.at(f.tgt_row)
    )
    exclude = {sid for sid in (src.section_id, tgt.section_id) if sid is not None}
    clearance = SECTION_ROUTE_CLEARANCE + ctx.curve_radius + EDGE_TO_BUNDLE_CLEARANCE
    # The descent column is nudged clear of every section its full band0->band1
    # span pierces, forced left of the source (bound_right) so the branch peels
    # off toward its down-and-left target rather than escaping right.
    corner_x = _clear_channel_x_in_band(
        graph, src.x, band0_y, band1_y, clearance, exclude, bound_right=src.x
    )
    if f.v_segment_crosses_other_section(corner_x, band0_y, band1_y, exclude):
        return None
    _fan, pos_n, delta, _cx = _wrap_fan_geometry(
        ctx, f.edge, src, f.i, f.n, Direction.D
    )
    vx = _left_entry_descent_x(ctx, tgt.x, pos_n)
    # Lead out into the inter-cell gap (right of the junction) so the turn down
    # curves clear of the source section edge; fall back to dropping at the
    # junction column when no cell-mate gap resolves.
    gap_mid = _exit_side_pack_gap_midpoint(f)
    lead_x = gap_mid if gap_mid is not None and gap_mid > src.x else src.x
    return _BandHopGeometry(band0_y, band1_y, lead_x, corner_x, vx, pos_n, delta)


def _left_entry_band_hop_source_seam(f: _InterFacts) -> _EntryWrapGeometry:
    """Resolve the seam shared by band-hop planning and emission.

    The hop opens on the same lead-out, turn and traverse as every other shape
    of the family; where the wrap proper leaves its band on the target's own
    descent column, the hop leaves it on the clear column between the two bands.
    See :func:`_route_left_entry_via_band_hop` for the rest of the shape.
    """
    geometry = _band_hop_geometry(f)
    assert geometry is not None
    return _entry_wrap_record(
        f.ctx,
        f.edge,
        f.src,
        pos_n=geometry.pos_n,
        delta=geometry.delta,
        corner_x=geometry.lead_x,
        channel_y=geometry.band0_y,
        descent_x=geometry.corner_x,
    )


def _left_entry_band_hop_is_clear(f: _InterFacts) -> bool:
    """Whether a LEFT-entry feed from a boxed-in fan junction can hop two bands.

    Used when the source -- a straddling fan-out junction seated between its own
    section and a packed cell-mate -- has its natural source-side descent column
    blocked (so :func:`_left_entry_gap_above_is_clear` defers) and the
    around-below loop would dive to the canvas bottom.  The branch instead drops
    into the band just below the source row, traverses to a clear inter-column
    gap, descends there into the band above the target row, then drops down the
    target's left side into the port.

    Viable only when the source is a fan-out junction boxed in by a packed
    cell-mate (a plain cross-row fan reaches its clear column via the gap-above
    path or the around-below dive instead), :func:`_band_hop_geometry` resolves,
    and none of the moving legs -- the source drop into band0, the band0
    traverse, the clear-column descent, the band1 traverse, the target-side
    descent -- crosses a section.
    """
    if not _source_is_boxed_fanout_junction(f):
        return False
    geom = _band_hop_geometry(f)
    if geom is None:
        return False
    src, tgt = f.src, f.tgt
    if f.v_segment_crosses_other_section(geom.lead_x, src.y, geom.band0_y):
        return False
    if f.h_segment_crosses_other_section(geom.lead_x, geom.corner_x, geom.band0_y):
        return False
    if f.h_segment_crosses_other_section(geom.corner_x, geom.vx, geom.band1_y):
        return False
    return not f.v_segment_crosses_other_section(geom.vx, geom.band1_y, tgt.y)


def _route_left_entry_via_band_hop(f: _InterFacts) -> RoutedPath:
    """Route a boxed-in fan branch to a LEFT entry by hopping two inter-row bands.

    The divergent branch of a straddling fan-out cannot descend at the trapped
    junction (every column there pierces the packed cell-mate or the row below).
    It drops into the band below the source row, traverses LEFT to a clear
    inter-column gap, descends that gap through the intervening rows into the
    band above the target row, then drops down the target's LEFT (outward) side
    into the port -- the "leave on the left" peel-off the around-below dive
    (:func:`_route_around_section_below`) can't take::

        (sx, sy)        -> H right into the inter-cell gap
        (lx, sy)        ; turn down (curve clear of the source edge)
        (lx, b0)        -> H left to the clear descent column
        (cx, b0)        ; V down through the intervening rows
        (cx, b1)        -> H left past the target's left edge
        (vx, b1)        ; V down to the entry Y
        (vx, ey)        -> H right into the LEFT port
        (ex, ey)

    Both channels and the descent column are resolved by
    :func:`_band_hop_geometry` (guaranteed non-None by
    :func:`_left_entry_band_hop_is_clear` at the call site).
    """
    edge, src, tgt, ctx = f.edge, f.src, f.tgt, f.ctx
    geom = _band_hop_geometry(f)
    assert geom is not None
    delta = geom.delta
    src_off = _get_offset(ctx, edge.source, edge.line_id)
    tgt_off = _get_offset(ctx, edge.target, edge.line_id)
    ex, ey = tgt.x, tgt.y
    entry_delta = delta  # a LEFT entry runs rightward in, like the target drop
    # Lead out of the junction (Y-staggered, matching the upstream feed's end)
    # into the inter-cell gap, then turn down so the departure curves clear of
    # the source section edge instead of kinking against it.
    centerline = [
        (src.x, src.y + src_off + delta),
        (geom.lead_x, src.y + src_off + delta),
        (geom.lead_x, geom.band0_y),
        (geom.corner_x, geom.band0_y),
        (geom.corner_x, geom.band1_y),
        (geom.vx, geom.band1_y),
        (geom.vx, ey + tgt_off + entry_delta),
        (ex, ey + tgt_off + entry_delta),
    ]
    route = route_along(
        edge,
        [(edge, edge.line_id, -delta)],
        centerline,
        base_radius=ctx.curve_radius,
        bundle_offsets=fan_offsets(geom.pos_n, ctx.offset_step),
    )
    assert route is not None
    _declare_channel(route, ctx, geom.corner_x, Direction.D)
    _declare_channel(route, ctx, geom.vx, vertical_direction(ey - geom.band1_y))
    return route


def _route_right_entry_around_below(f: _InterFacts) -> RoutedPath:
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
    edge, src, entry_port, ctx = f.edge, f.src, f.tgt, f.ctx
    src_col, src_row = f.src_col, f.src_row
    ep_col = f.tgt_col
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
        edge, src, entry_port, f.i, f.n, ctx, channel_y_base
    )
