"""A symmetric-mode multi-track merge anchors on the median of its feeders.

When 3+ metro lines on distinct tracks converge on a single shared station and
no single predecessor already carries the full bundle, the station is a genuine
multi-track merge.  In ``diamond_style: symmetric`` the merge must sit on the
median predecessor track so each feeder bends toward it by the least amount --
not snap to the first-declared line's (extreme) track, which forces every other
feeder into a longer detour (#1277).
"""

from __future__ import annotations

from pathlib import Path
from statistics import median_low

import networkx as nx
import pytest

from nf_metro.layout.constants import SAME_COORD_TOLERANCE
from nf_metro.layout.engine import compute_layout
from nf_metro.parser.mermaid import parse_metro_mermaid

_TOPOLOGIES = Path(__file__).resolve().parents[1] / "examples" / "topologies"

# symmetric_multiline_merge_median is the minimal repro; rowmate carries the
# real-world analogue (umi_tools_dedup merging three aligners); the last two
# have no genuine multi-track merge and exercise the invariant vacuously.
_SYMMETRIC_FIXTURES = [
    "symmetric_multiline_merge_median",
    "rowmate_tb_side_entry_top_align",
    "fork_join_interior_label",
    "symmetric_diamond_beside_wide_fan",
]


def _layout(name: str):
    graph = parse_metro_mermaid((_TOPOLOGIES / f"{name}.mmd").read_text())
    compute_layout(graph)
    return graph


def _convergence_merges(graph) -> list[tuple[str, list[float]]]:
    """Return (station_id, sorted predecessor Ys) for each genuine multi-track merge.

    A genuine merge has >1 predecessor, carries more lines than any single
    predecessor, and no predecessor already carries its full line bundle (which
    would be a trunk junction, not a convergence).
    """
    G: nx.DiGraph[str] = nx.DiGraph()
    for edge in graph.edges:
        G.add_edge(edge.source, edge.target)
    merges: list[tuple[str, list[float]]] = []
    for sid, station in graph.stations.items():
        if station.is_port:
            continue
        preds = list(G.predecessors(sid))
        if len(preds) <= 1:
            continue
        node_lines = set(graph.station_lines(sid))
        if not node_lines:
            continue
        pred_line_sets = [set(graph.station_lines(p)) for p in preds]
        if len(node_lines) <= max(len(pls) for pls in pred_line_sets):
            continue
        if any(pls == node_lines for pls in pred_line_sets):
            continue
        pred_ys = sorted(graph.stations[p].y for p in preds if p in graph.stations)
        merges.append((sid, pred_ys))
    return merges


@pytest.mark.parametrize("name", _SYMMETRIC_FIXTURES)
def test_symmetric_merge_sits_on_median_feeder_track(name: str) -> None:
    graph = _layout(name)
    for sid, pred_ys in _convergence_merges(graph):
        expected = median_low(pred_ys)
        actual = graph.stations[sid].y
        assert abs(actual - expected) <= SAME_COORD_TOLERANCE, (
            f"{name}: merge {sid!r} sits at y={actual:.1f}, not on the median "
            f"feeder track y={expected:.1f} (feeders {pred_ys}) -- snapped to an "
            "extreme track, forcing other feeders into longer detours"
        )


def test_repro_fixture_has_a_genuine_multitrack_merge() -> None:
    """The repro fixture must actually exercise the convergence branch."""
    merges = _convergence_merges(_layout("symmetric_multiline_merge_median"))
    assert any(len(pred_ys) >= 3 for _sid, pred_ys in merges), (
        "repro fixture no longer contains a 3+ feeder multi-track merge"
    )
