"""No fan may take a station that heads a fan of its own as a follower (#1842).

``assign_tracks`` seats a station that forks to two or more on-track successors
inside its section on the centre of *that* fan.  Trunk-follower discovery took
the station one hop along a fan's approach or departure path without asking
whether it already held such a centre, which put a fork apex on a second fan's
centreline and dragged it off the row its own branches fan out from.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nf_metro.layout.constants import SAME_Y_TOLERANCE
from nf_metro.layout.engine import compute_layout
from nf_metro.parser import parse_metro_mermaid
from nf_metro.parser.model import MetroGraph

FIXTURES = Path(__file__).parent / "fixtures"
EXAMPLES = Path(__file__).parent.parent / "examples"

# The `alignment` section of the nf-core/riboseq map: `umi_dedup` forks to
# `genomecov` and `salmon_quant`, and `salmon_quant` forks again.
RIBOSEQ = FIXTURES / "curve_invariant_repros" / "riboseq_inter_row_corridor.mmd"

# A diamond whose trunk continues past the join into a fork apex, which reaches
# the same trunk-follower discovery from the join side rather than the fork side.
JOIN_SIDE_APEX = """\
%%metro title: join-side apex
%%metro diamond_style: symmetric
%%metro line: l1 | Line one | #e6007e

graph LR
    subgraph s1 [Stage one]
        a[A]
        b[B]
        c[C]
        j[J]
        k[K]
        m[M]
        n[N]

        a -->|l1| b
        a -->|l1| c
        b -->|l1| j
        c -->|l1| j
        j -->|l1| k
        k -->|l1| m
        k -->|l1| n
    end

    subgraph s2 [Stage two]
        z[Z]
        y[Y]
        z -->|l1| y
    end

    m -->|l1| z
    n -->|l1| z
"""

# Maps whose fans reach an apex from the fork side (`riboseq_inter_row_corridor`,
# `rnaseq_sections`), from the join side (`sarek_metro`, `epitopeprediction`), or
# from both (`variantbenchmarking`).
FAN_HEAVY_MAPS = [
    RIBOSEQ,
    EXAMPLES / "sarek_metro.mmd",
    EXAMPLES / "epitopeprediction.mmd",
    EXAMPLES / "rnaseq_sections.mmd",
    EXAMPLES / "differentialabundance.mmd",
    EXAMPLES / "variantbenchmarking.mmd",
    EXAMPLES / "genomeassembly.mmd",
    EXAMPLES / "longread_variant_calling.mmd",
]


def _laid_out(text: str) -> MetroGraph:
    graph = parse_metro_mermaid(text)
    compute_layout(graph, validate=False)
    return graph


def _fork_apexes(graph: MetroGraph) -> set[str]:
    """Stations forking to two or more on-track successors in their own section.

    Restated from graph primitives rather than imported from the layout, so the
    assertion is not the production predicate checked against itself.
    """
    apexes: set[str] = set()
    for station_id in graph.stations:
        if station_id in graph.ports or station_id in graph.junction_ids:
            continue
        section_id = graph.section_for_station(station_id)
        successors = {
            edge.target
            for edge in graph.edges_from(station_id)
            if edge.target not in graph.ports
            and edge.target not in graph.junction_ids
            and graph.section_for_station(edge.target) == section_id
            and (target := graph.stations.get(edge.target)) is not None
            and not target.off_track
        }
        if len(successors) >= 2:
            apexes.add(station_id)
    return apexes


def _apex_followers(graph: MetroGraph) -> list[tuple[str, str]]:
    """``(plan fork, apex)`` for every fork apex a fan holds as a trunk follower."""
    apexes = _fork_apexes(graph)
    return [
        (plan.fork_station_id, station_id)
        for plan in graph.fan_plans
        for station_id in plan.trunk_follower_ids
        if station_id in apexes
    ]


@pytest.mark.parametrize("path", FAN_HEAVY_MAPS, ids=lambda p: p.stem)
def test_no_fan_takes_a_fork_apex_as_a_trunk_follower(path: Path) -> None:
    assert _apex_followers(_laid_out(path.read_text())) == []


def test_join_side_trunk_follower_apex_is_not_claimed() -> None:
    assert _apex_followers(_laid_out(JOIN_SIDE_APEX)) == []


def test_fork_apex_keeps_the_section_trunk_row() -> None:
    """`star -> umi_dedup` runs level, with the fan symmetric about the apex."""
    graph = _laid_out(RIBOSEQ.read_text())
    entry_y = next(
        graph.stations[port_id].y
        for port_id, port in graph.ports.items()
        if port.section_id == "alignment" and port.is_entry
    )
    apex_y = graph.stations["umi_dedup"].y
    assert abs(graph.stations["star"].y - entry_y) <= SAME_Y_TOLERANCE
    assert abs(apex_y - entry_y) <= SAME_Y_TOLERANCE

    above = graph.stations["genomecov"].y
    below = graph.stations["salmon_quant"].y
    assert abs((above + below) / 2 - apex_y) <= SAME_Y_TOLERANCE
    assert abs(below - above) > SAME_Y_TOLERANCE
