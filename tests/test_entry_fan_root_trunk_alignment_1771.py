"""An entry port feeding a section root plus a deeper arm sits on the root.

When one entry port carries one line straight to the section's root station and
a second line straight to an arm the root itself reaches (a fan that branches at
the section boundary), the port must sit on the root's row.  Seating it on the
deeper arm instead pins the root's line off its row, forcing an up-then-down
overshoot right at the boundary that nets zero vertical change -- the #1771
"trunk anchored one grid unit too low" defect.

The port feeds ``fasta`` (the QC section's root, reached by the ``reference``
line) and ``kmer`` (a deeper arm the ``short`` line reaches, itself fed by
``fasta``).  The port belongs on ``fasta``'s row so ``reference`` runs straight
into the trunk and ``short`` drops off it to ``kmer``.
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
    / "packed_cell_right_exit_left_entry_wrap.mmd"
)

TOL = 2.0


def _layout():
    graph = parse_metro_mermaid(FIXTURE.read_text())
    compute_layout(graph)
    return graph


def test_qc_entry_port_seats_on_root_not_deeper_arm():
    graph = _layout()
    st = graph.stations

    section = graph.sections["qc"]
    entry_ports = [
        pid
        for pid in section.entry_ports
        if (port := graph.ports.get(pid)) and port.side == PortSide.LEFT
    ]
    assert len(entry_ports) == 1, entry_ports
    port_y = st[entry_ports[0]].y

    assert abs(port_y - st["fasta"].y) < TOL, (
        f"entry port y={port_y} not flush with root station fasta "
        f"y={st['fasta'].y}: the reference line doglegs into the trunk"
    )
    assert abs(port_y - st["kmer"].y) > TOL, (
        f"entry port y={port_y} still seated on the deeper arm kmer y={st['kmer'].y}"
    )


def test_qc_internal_fan_geometry_unchanged():
    graph = _layout()
    st = graph.stations

    # kmer is one lane above the fasta/quast trunk row; busco one lane below.
    lane = st["fasta"].y - st["kmer"].y
    assert lane > TOL, (st["kmer"].y, st["fasta"].y)
    assert abs(st["quast"].y - st["fasta"].y) < TOL
    assert abs((st["busco"].y - st["fasta"].y) - lane) < TOL
