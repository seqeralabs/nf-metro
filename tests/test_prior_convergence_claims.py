"""Right-entry planning sees only convergence geometry available in sequence."""

from types import MappingProxyType, SimpleNamespace

from nf_metro.layout.route_plan import RouteSystemId
from nf_metro.layout.routing.common import RoutedPath
from nf_metro.layout.routing.convergences import (
    ConvergencePlanExecutionQuery,
    PlannedConvergenceVerticalChannel,
)
from nf_metro.layout.routing.inter_section_handlers import (
    _leadout_self_meets_sibling_descent,
)
from nf_metro.parser.model import Edge
from nf_metro.parser.route_topology import ResolvedEdge


def _decision(ctx, edge: Edge) -> bool:
    return _leadout_self_meets_sibling_descent(
        ctx,
        edge,
        corner_x=10.0,
        y_lo=0.0,
        y_hi=100.0,
        gap_right=30.0,
    )


def test_planned_convergence_claim_precedes_wrap_in_both_passes() -> None:
    convergence_edge = ResolvedEdge("merge-source", "merge-target", "main")
    wrap_edge = ResolvedEdge("wrap-source", "wrap-target", "main")
    future_edge = ResolvedEdge("future-source", "future-target", "main")
    prior_claim = PlannedConvergenceVerticalChannel(
        RouteSystemId("prior-system"),
        convergence_edge,
        convergence_edge.source,
        convergence_edge.line_id,
        0,
        0,
        20.0,
        0.0,
        100.0,
    )
    future_claim = PlannedConvergenceVerticalChannel(
        RouteSystemId("future-system"),
        future_edge,
        future_edge.source,
        future_edge.line_id,
        2,
        0,
        12.0,
        0.0,
        100.0,
    )
    query = ConvergencePlanExecutionQuery(
        (),
        MappingProxyType({}),
        (convergence_edge, wrap_edge, future_edge),
        (prior_claim, future_claim),
    )
    edge = Edge(wrap_edge.source, wrap_edge.target, wrap_edge.line_id)
    planning_ctx = SimpleNamespace(built_routes=[], convergences=query)
    production_ctx = SimpleNamespace(built_routes=[], convergences=query)

    assert query.prior_vertical_channels_for_edge(edge) == (prior_claim,)
    assert _decision(planning_ctx, edge)
    assert _decision(production_ctx, edge) == _decision(planning_ctx, edge)


def test_compatibility_convergence_route_precedes_wrap_in_both_passes() -> None:
    convergence_edge = ResolvedEdge("merge-source", "merge-target", "main")
    wrap_edge = ResolvedEdge("wrap-source", "wrap-target", "main")
    query = ConvergencePlanExecutionQuery(
        (), MappingProxyType({}), (convergence_edge, wrap_edge), ()
    )
    compatibility_route = RoutedPath(
        Edge(
            convergence_edge.source,
            convergence_edge.target,
            convergence_edge.line_id,
        ),
        convergence_edge.line_id,
        [(20.0, 0.0), (20.0, 100.0)],
        is_inter_section=True,
    )
    edge = Edge(wrap_edge.source, wrap_edge.target, wrap_edge.line_id)
    planning_ctx = SimpleNamespace(
        built_routes=[compatibility_route], convergences=query
    )
    production_ctx = SimpleNamespace(
        built_routes=[compatibility_route], convergences=query
    )

    assert query.prior_vertical_channels_for_edge(edge) == ()
    assert _decision(planning_ctx, edge)
    assert _decision(production_ctx, edge) == _decision(planning_ctx, edge)
