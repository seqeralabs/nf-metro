"""The generic bypass keeps a far LEFT-entry feed outside every section."""

from pathlib import Path

from nf_metro.api import prepare_graph
from nf_metro.layout.engine import compute_layout
from nf_metro.layout.phases.guards import routes_through_unrelated_sections
from nf_metro.layout.routing import compute_station_offsets, route_edges_centred
from nf_metro.parser.mermaid import parse_metro_mermaid
from nf_metro.render.svg import build_render_plan
from nf_metro.themes import resolve_theme


def test_bypass_into_far_left_entry_clears_sections() -> None:
    path = (
        Path(__file__).resolve().parents[1]
        / "examples/topologies/bypass_left_entry_from_right.mmd"
    )
    graph = parse_metro_mermaid(path.read_text())
    compute_layout(graph)
    offsets = compute_station_offsets(graph)
    routes = route_edges_centred(graph, station_offsets=offsets)
    offenders = routes_through_unrelated_sections(graph, routes=routes, offsets=offsets)
    assert not offenders, "\n".join(
        f"line {route.line_id!r} {route.edge.source!r}->{route.edge.target!r} "
        f"passes through section {section_id!r}"
        for route, section_id in offenders
    )


def test_lines_parting_at_the_fork_descend_in_their_departure_order() -> None:
    """``main`` and ``side`` leave ``__junction_3`` westward on its lanes,
    ``main`` on the north one, and share one descent in the gap before
    ``main`` peels west into its loop and ``side`` runs on down to Sibling.

    The descent carries the fork's order round its corner: the north lane
    turns outside, west of ``side``, so ``main`` peels away on the side it
    turns to without crossing ``side``, and the layout passes its own final
    guards.
    """
    path = (
        Path(__file__).resolve().parents[1]
        / "examples/topologies/bypass_left_entry_from_right.mmd"
    )
    graph = prepare_graph(path.read_text())
    plan = build_render_plan(graph, resolve_theme("nfcore", graph, "light"))
    branches = {
        route.line_id: points
        for route, points in zip(plan.routes, plan.route_polylines)
        if route.edge.source == "__junction_3"
    }
    (_, lane_main), (column_main, _) = branches["main"][0], branches["main"][1]
    (_, lane_side), (column_side, _) = branches["side"][0], branches["side"][1]
    assert lane_main < lane_side
    assert column_main < column_side

    compute_layout(parse_metro_mermaid(path.read_text()), validate=True)
