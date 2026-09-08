"""Entry-port same-line fan legs peel in a nested order.

Issue #1831: when several same-line branches leave an LR/RL entry port toward
stacked internal stations, the deeper legs must start their diagonal earlier so
the opening reads as a staggered staircase rather than several rays from one
turn vertex.
"""

from __future__ import annotations

from pathlib import Path

from nf_metro.layout.engine import compute_layout
from nf_metro.layout.routing import compute_station_offsets, route_edges
from nf_metro.parser.mermaid import parse_metro_mermaid

REPO_ROOT = Path(__file__).resolve().parent.parent
RIBOSEQ = (
    REPO_ROOT
    / "tests"
    / "fixtures"
    / "curve_invariant_repros"
    / "riboseq_inter_row_corridor.mmd"
)

SYNTHETIC_ENTRY_PORT_FORK = """\
%%metro title: port fork repro
%%metro center_ports: true
%%metro line: l | L | #ff00aa
%%metro grid: src, sink | 0,0

graph LR
    subgraph src [Source]
        seed[Seed]
    end
    subgraph sink [Sink]
        a[A]
        b[B]
        c[C]
        d[D]
    end
    seed -->|l| a
    seed -->|l| b
    seed -->|l| c
    seed -->|l| d
"""


def _routed_fixture(path: Path):
    graph = parse_metro_mermaid(path.read_text())
    graph.source_dir = str(path.parent)
    compute_layout(graph)
    offsets = compute_station_offsets(graph)
    return graph, route_edges(graph, station_offsets=offsets)


def _routed_text(text: str):
    graph = parse_metro_mermaid(text)
    compute_layout(graph)
    offsets = compute_station_offsets(graph)
    return graph, route_edges(graph, station_offsets=offsets)


def _opening_leads(
    graph,
    routes,
    *,
    source_id: str,
    line_id: str,
    sign: int,
) -> list[tuple[str, float, float]]:
    source = graph.stations[source_id]
    leads: list[tuple[str, float, float]] = []
    for rp in routes:
        edge = rp.edge
        if edge.source != source_id or edge.line_id != line_id or len(rp.points) < 4:
            continue
        target = graph.stations[edge.target]
        drop = target.y - source.y
        if sign * drop <= 0:
            continue
        lead_x = rp.points[1][0]
        leads.append((edge.target, abs(drop), lead_x))
    leads.sort(key=lambda item: (-item[1], item[0]))
    return leads


def _assert_nested_opening_leads(leads: list[tuple[str, float, float]]) -> None:
    assert len(leads) >= 2, "expected at least two diagonal sibling legs"
    for (deep_id, deep_drop, deep_x), (shallow_id, shallow_drop, shallow_x) in zip(
        leads, leads[1:]
    ):
        assert deep_drop > shallow_drop
        assert deep_x < shallow_x - 0.5, (
            f"{deep_id} (|dy|={deep_drop:.1f}) and {shallow_id} "
            f"(|dy|={shallow_drop:.1f}) open at x={deep_x:.1f} and x={shallow_x:.1f}; "
            "deeper entry-port fork legs should peel earlier so the opening nests"
        )


def test_riboseq_entry_port_fork_peels_on_both_sides() -> None:
    graph, routes = _routed_fixture(RIBOSEQ)

    down = _opening_leads(
        graph, routes, source_id="orf_calling__entry_left_7", line_id="riboseq", sign=1
    )
    up = _opening_leads(
        graph, routes, source_id="orf_calling__entry_left_7", line_id="riboseq", sign=-1
    )

    _assert_nested_opening_leads(down)
    _assert_nested_opening_leads(up)


def test_synthetic_entry_port_same_line_fan_peels_by_depth() -> None:
    graph, routes = _routed_text(SYNTHETIC_ENTRY_PORT_FORK)

    leads = _opening_leads(
        graph, routes, source_id="sink__entry_left_1", line_id="l", sign=1
    )

    _assert_nested_opening_leads(leads)
