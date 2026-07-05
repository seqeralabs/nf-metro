"""Two-direction entry into a folded return-row section (#1341 / #1342).

A folded Ribo-seq excerpt: ``psite_id`` sits on the RL return row, fed from
``quantification`` directly above it and ``orf_calling`` to its right.

The hint-free variant is the goal state -- entry-side inference (#1342) picks a
single sensible side from the feed geometry and the map lays out cleanly.  The
hand-tuned variant (``riboseq_fold_two_dir_entry.mmd``) pins the grid and forces
``entry: top``; it renders but exhibits the route-around and overlap defects
tracked by #1343 (exit-port re-pin), #1344 (overlap-free placement) and
#1341 goal 2 (off-side route-around), caught by the corpus invariant suite until
those land.
"""

from __future__ import annotations

import sys
import warnings
from pathlib import Path

from nf_metro.api import prepare_graph, render_string
from nf_metro.layout.engine import compute_layout
from nf_metro.layout.routing.core import route_edges
from nf_metro.parser.mermaid import parse_metro_mermaid
from nf_metro.parser.model import MetroGraph, PortSide

sys.path.insert(0, str(Path(__file__).parent))
from layout_validator import check_section_overlap  # noqa: E402

HINTLESS = "examples/topologies/riboseq_fold_two_dir_entry_hintless.mmd"
HINTED = "examples/topologies/riboseq_fold_two_dir_entry.mmd"


def _layout(path: str, *, validate: bool, strict: bool = False) -> MetroGraph:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        graph = parse_metro_mermaid(open(path).read())
    graph.strict = strict
    compute_layout(graph, validate=validate)
    return graph


def test_hintless_lays_out_under_strict() -> None:
    """The hint-free variant lays out without raising, even under strict."""
    _layout(HINTLESS, validate=True, strict=True)


def test_hintless_psite_infers_single_sensible_entry_side() -> None:
    """psite_id (fed from above + right) infers one entry side, the fed RIGHT.

    With no ``entry:`` hint the return row resolves to RL and the entry side is
    inferred from the feed geometry: RIGHT is both the flow-natural side and a
    side a feed reaches, so it is chosen and shared by every entering line.
    """
    graph = _layout(HINTLESS, validate=False)
    sides = {graph.ports[pid].side for pid in graph.sections["psite_id"].entry_ports}
    assert sides == {PortSide.RIGHT}


def test_hintless_no_section_overlap() -> None:
    """The hint-free variant places every section box clear of its neighbours."""
    graph = _layout(HINTLESS, validate=False)
    overlaps = check_section_overlap(graph)
    assert not overlaps, "\n".join(v.message for v in overlaps)


def test_hinted_renders_without_curve_abort() -> None:
    """The hand-tuned variant renders without tripping the curve invariants.

    ``quantification`` fans one ``ribo`` line to two TOP entries: ``psite_id``
    directly below and ``orf_calling`` below-and-to-the-side.  A drop-first route
    into the side entry would descend in a fan lane parallel to the psite drop
    and abort on the same-line parallel-descent guard.
    """
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        render_string(open(HINTED).read())


def test_hinted_side_fan_branch_traverses_before_dropping() -> None:
    """The below-side fan branch reaches its TOP port by one clean drop.

    The ``quantification -> orf_calling`` feed must traverse at the source row's
    Y to the target descent column, then drop straight into the port -- so its
    only vertical descent sits at the port X, not in a fan lane beside the
    ``quantification -> psite_id`` descent one column to the left.
    """
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        graph = prepare_graph(open(HINTED).read())
    compute_layout(graph)
    routes = route_edges(graph)

    orf_port = graph.stations["orf_calling__entry_top_7"]
    psite_port = graph.stations["psite_id__entry_top_8"]

    orf_feed = next(
        r
        for r in routes
        if r.edge.target == orf_port.id and r.edge.source in graph.junction_ids
    )

    def _vertical_xs(route) -> list[float]:
        pts = route.points
        return [
            pts[i][0]
            for i in range(len(pts) - 1)
            if abs(pts[i][0] - pts[i + 1][0]) < 1.0
            and abs(pts[i][1] - pts[i + 1][1]) > 1.0
        ]

    orf_descents = _vertical_xs(orf_feed)
    psite_descent = psite_port.x

    assert orf_descents, "orf feed has no vertical descent"
    # Every vertical descent of the orf feed sits at its own port X, clear of the
    # psite descent one column to the left.
    for x in orf_descents:
        assert abs(x - orf_port.x) < 1.0, (
            f"orf feed descends at x={x:.0f}, not at its port x={orf_port.x:.0f}"
        )
        assert abs(x - psite_descent) > 1.0, (
            f"orf feed descends at x={x:.0f}, parallel to the psite descent "
            f"x={psite_descent:.0f}"
        )
