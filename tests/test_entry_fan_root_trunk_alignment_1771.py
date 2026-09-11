"""An entry port feeding a section root plus a deeper arm sits on the root.

When one entry port carries one line straight to the section's root station and
a second line straight to an arm the root itself reaches (a fan that branches at
the section boundary), the port must sit on the root's row.  Seating it on the
deeper arm instead pins the root's line off its row, forcing an up-then-down
overshoot right at the boundary that nets zero vertical change -- the #1771
"trunk anchored one grid unit too low" defect.

``entry_fan_root_vs_deeper_arm.mmd`` isolates the shape: the ``work`` entry port
feeds ``root`` (via the ``main`` line) and ``arm1`` (via the ``branch`` line),
and ``root`` itself feeds ``arm1``.  ``packed_cell_right_exit_left_entry_wrap.mmd``
is the full pipeline the defect was first found in, where the port feeds ``fasta``
(the QC section's root) and ``kmer`` (a deeper arm ``fasta`` feeds).  In both the
port belongs on the root's row so the root line runs straight into the trunk and
the deeper-arm line drops off it.
"""

from __future__ import annotations

from pathlib import Path

from nf_metro.layout.engine import compute_layout
from nf_metro.parser.mermaid import parse_metro_mermaid
from nf_metro.parser.model import PortSide

TOPOLOGIES = Path(__file__).parent.parent / "examples" / "topologies"
MINIMAL_FIXTURE = TOPOLOGIES / "entry_fan_root_vs_deeper_arm.mmd"
CORPUS_FIXTURE = TOPOLOGIES / "packed_cell_right_exit_left_entry_wrap.mmd"

TOL = 2.0


def _layout(fixture: Path):
    graph = parse_metro_mermaid(fixture.read_text())
    compute_layout(graph)
    return graph


def _sole_left_entry_y(graph, section_id: str) -> float:
    section = graph.sections[section_id]
    entry_ports = [
        pid
        for pid in section.entry_ports
        if (port := graph.ports.get(pid)) and port.side == PortSide.LEFT
    ]
    assert len(entry_ports) == 1, entry_ports
    return graph.stations[entry_ports[0]].y


def test_minimal_entry_port_seats_on_root_not_deeper_arm():
    graph = _layout(MINIMAL_FIXTURE)
    st = graph.stations
    port_y = _sole_left_entry_y(graph, "work")

    assert abs(port_y - st["root"].y) < TOL, (
        f"entry port y={port_y} not flush with root station root "
        f"y={st['root'].y}: the main line doglegs into the trunk"
    )
    assert abs(port_y - st["arm1"].y) > TOL, (
        f"entry port y={port_y} still seated on the deeper arm arm1 y={st['arm1'].y}"
    )


def test_minimal_internal_fan_geometry():
    graph = _layout(MINIMAL_FIXTURE)
    st = graph.stations

    # root feeds arm1 above and arm2 below its own trunk row.
    assert st["arm1"].y < st["root"].y - TOL, (st["arm1"].y, st["root"].y)
    assert st["arm2"].y > st["root"].y + TOL, (st["arm2"].y, st["root"].y)


def test_qc_entry_port_seats_on_root_not_deeper_arm():
    graph = _layout(CORPUS_FIXTURE)
    st = graph.stations
    port_y = _sole_left_entry_y(graph, "qc")

    assert abs(port_y - st["fasta"].y) < TOL, (
        f"entry port y={port_y} not flush with root station fasta "
        f"y={st['fasta'].y}: the reference line doglegs into the trunk"
    )
    assert abs(port_y - st["kmer"].y) > TOL, (
        f"entry port y={port_y} still seated on the deeper arm kmer y={st['kmer'].y}"
    )


def test_qc_internal_fan_geometry_unchanged():
    graph = _layout(CORPUS_FIXTURE)
    st = graph.stations

    # kmer is one lane above the fasta/quast trunk row; busco one lane below.
    lane = st["fasta"].y - st["kmer"].y
    assert lane > TOL, (st["kmer"].y, st["fasta"].y)
    assert abs(st["quast"].y - st["fasta"].y) < TOL
    assert abs((st["busco"].y - st["fasta"].y) - lane) < TOL
