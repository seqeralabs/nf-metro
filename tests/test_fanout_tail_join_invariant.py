"""Tests for the fan-out junction tail-join invariant.

At a fan-out junction (single upstream source, one or more outgoing
inter-section targets), the upstream ``port -> junction`` route must
end exactly where the paired same-line ``junction -> target`` route
begins, so the corner renders as one continuous line rather than two
segments meeting end-to-end with a notch / seam.

Covers:

* Happy-path: every gallery fixture and example routes with zero
  fan-out tail gaps.
* Targeted: ``variant_calling_tuned`` ``__junction_6`` (the reported
  defect) joins continuously for both ``main`` and ``qc``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nf_metro.layout.engine import compute_layout
from nf_metro.layout.routing import compute_station_offsets, route_edges
from nf_metro.layout.routing.invariants import (
    check_fanout_tail_join,
    fanout_junctions,
)
from nf_metro.parser.mermaid import parse_metro_mermaid

REPO_ROOT = Path(__file__).resolve().parent.parent
TOPOLOGIES = REPO_ROOT / "tests" / "fixtures" / "topologies"
EXAMPLES = REPO_ROOT / "examples"


def _gather_fixtures() -> list[Path]:
    paths: list[Path] = []
    paths.extend(sorted(TOPOLOGIES.glob("*.mmd")))
    paths.extend(sorted(EXAMPLES.glob("*.mmd")))
    return paths


def _route(path: Path):
    graph = parse_metro_mermaid(path.read_text())
    compute_layout(graph)
    routes = route_edges(graph, station_offsets=compute_station_offsets(graph))
    return graph, routes


@pytest.mark.parametrize(
    "path", _gather_fixtures(), ids=lambda p: p.relative_to(REPO_ROOT).as_posix()
)
def test_no_fanout_tail_gaps_in_gallery(path: Path) -> None:
    """Every shipped topology and example routes without a fan-out
    junction tail gap (continuous upstream/downstream handoff)."""
    graph, routes = _route(path)
    gaps = check_fanout_tail_join(routes, graph)
    assert not gaps, "\n".join(g.message() for g in gaps)


def test_variant_calling_tuned_junction6_joins() -> None:
    """The reported defect: __junction_6 in variant_calling_tuned, where
    main (green) and qc (blue) fan out together, must hand off
    continuously between the upstream and downstream routes."""
    path = EXAMPLES / "variant_calling_tuned.mmd"
    graph, routes = _route(path)

    # Sanity: __junction_6 is a genuine single-source fan-out junction.
    fanouts = fanout_junctions(graph)
    assert "__junction_6" in fanouts

    gaps = check_fanout_tail_join(routes, graph)
    j6_gaps = [g for g in gaps if g.junction_id == "__junction_6"]
    assert not j6_gaps, "\n".join(g.message() for g in j6_gaps)


def test_merge_junctions_excluded_from_fanout() -> None:
    """Merge junctions (>1 upstream source) must NOT be treated as
    fan-out junctions, so their trunk routing is never snapped."""
    # collector-fan-in-style fixtures carry merge junctions; assert any junction
    # with multiple distinct upstream sources is excluded.
    for path in _gather_fixtures():
        graph = parse_metro_mermaid(path.read_text())
        compute_layout(graph)
        fanouts = fanout_junctions(graph)
        for jid in graph.junction_ids:
            sources = {e.source for e in graph.edges_to(jid)}
            if len(sources) > 1:
                assert jid not in fanouts, (
                    f"{path.name}: merge junction {jid} "
                    f"({len(sources)} sources) wrongly classified as fan-out"
                )
