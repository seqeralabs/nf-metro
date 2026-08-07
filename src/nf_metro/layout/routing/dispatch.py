"""Shared first-match routing dispatch for one edge."""

from __future__ import annotations

from typing import TYPE_CHECKING

from nf_metro.layout.routing.common import RoutedPath
from nf_metro.layout.routing.context import _RoutingCtx
from nf_metro.layout.routing.families import RouteFamilyId
from nf_metro.layout.routing.inter_section_handlers import _route_inter_section
from nf_metro.layout.routing.intra_handlers import (
    _route_entry_runway,
    _route_intra_section,
)
from nf_metro.layout.routing.tb_handlers import _route_tb_section
from nf_metro.parser.model import Edge

if TYPE_CHECKING:
    from nf_metro.layout.route_plan import RoutePlanObserver


def route_edge_by_handler_priority(
    edge: Edge,
    ctx: _RoutingCtx,
    *,
    observer: RoutePlanObserver | None = None,
    planned_family_id: RouteFamilyId | None = None,
) -> RoutedPath | None:
    """Run the canonical first-match handler chain for one edge."""
    source, target = ctx.graph.edge_endpoints(edge)
    observe_fallback = (
        observer is not None
        and (source.is_port or edge.source in ctx.junction_ids)
        and (target.is_port or edge.target in ctx.junction_ids)
    )
    route = _route_inter_section(
        edge,
        source,
        target,
        ctx,
        observer=observer,
        planned_family_id=planned_family_id,
    )
    fallback_handlers = (
        (_route_tb_section, RouteFamilyId.TB_SECTION_FALLBACK),
        (_route_entry_runway, RouteFamilyId.ENTRY_RUNWAY_FALLBACK),
        (_route_intra_section, RouteFamilyId.INTRA_SECTION_FALLBACK),
    )
    for handler, family_id in fallback_handlers:
        if route is not None:
            break
        route = handler(edge, source, target, ctx)
        if route is not None and observe_fallback:
            assert observer is not None
            observer.record_dispatch(
                (edge.source, edge.target, edge.line_id), family_id
            )
    return route
