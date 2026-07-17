"""Section trunk selection: the long main chain, not a short off-track
output branch, must ride the section trunk (#1487).

Inside a horizontal section with a long internal chain plus a short branch
feeding an off-track output file, the long chain is the section's through-line
and must lie straight on the trunk (the LEFT/RIGHT port Y). The short output
branch must peel off the trunk rather than take it, so the main chain is never
forced onto a diagonal that crosses the output branch.
"""

from __future__ import annotations

from pathlib import Path

from nf_metro.layout.engine import compute_layout
from nf_metro.parser.mermaid import parse_metro_mermaid
from nf_metro.parser.model import PortSide

FIXTURE = (
    Path(__file__).parent.parent
    / "examples"
    / "topologies"
    / "section_trunk_short_output_branch.mmd"
)

TOL = 2.0


def _layout():
    graph = parse_metro_mermaid(FIXTURE.read_text())
    compute_layout(graph)
    return graph


def _trunk_y(graph, section_id: str) -> float:
    section = graph.sections[section_id]
    for pid in section.entry_ports + section.exit_ports:
        port = graph.ports.get(pid)
        st = graph.stations.get(pid)
        if port and st and port.side in (PortSide.LEFT, PortSide.RIGHT):
            return st.y
    raise AssertionError(f"no LEFT/RIGHT port on {section_id}")


def test_main_chain_rides_trunk_short_output_peels():
    graph = _layout()
    st = graph.stations
    trunk_y = _trunk_y(graph, "main_sec")

    # The main-chain spine (s2 -> s3) lies straight on the section trunk.
    assert abs(st["s2"].y - trunk_y) < TOL, (
        f"main-chain s2 off trunk: y={st['s2'].y} trunk={trunk_y}"
    )
    assert abs(st["s3"].y - trunk_y) < TOL, (
        f"main-chain s3 off trunk: y={st['s3'].y} trunk={trunk_y}"
    )
    assert abs(st["s2"].y - st["s3"].y) < TOL, (
        f"main chain s2->s3 not axis-aligned: {st['s2'].y} vs {st['s3'].y}"
    )

    # The short output branch (s1 -> sink) peels off the trunk.
    assert abs(st["s1"].y - trunk_y) > TOL, (
        f"short output branch s1 sits on trunk: y={st['s1'].y} trunk={trunk_y}"
    )

    # The s3 fork (s4/s5) straddles the trunk symmetrically, so the through
    # chain is not dragged onto a diagonal to reach either arm.
    assert abs((st["s4"].y - trunk_y) + (st["s5"].y - trunk_y)) < TOL, (
        f"s4/s5 fork not symmetric about trunk: "
        f"s4={st['s4'].y} s5={st['s5'].y} trunk={trunk_y}"
    )
