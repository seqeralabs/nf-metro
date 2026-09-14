"""A symmetric entry fork carrying trunk-row bypass traffic keeps full pitch.

`_section_has_symmetric_entry_fork` compacts a two-way `diamond_style: symmetric`
fork onto half-pitch, leaving the trunk row empty between the branches.  That is
only correct when the trunk row *is* empty.  On the riboseq map the alignment
fork `umi_dedup -> {genomecov, salmon_quant}` is not: `umi_dedup` also runs the
`{rnaseq, riboseq}` bundle straight down the trunk row to the section's exit
port, bypassing both branches, so the row carries real traffic.  Compacting that
fork crowds three lanes (two branches plus the bypass bundle) into two rows'
worth of space.

The eligibility check excludes a fork whose shared hub has a direct edge to an
LR exit port of the section (bypass traffic on the trunk row), so the fork keeps
full pitch.  A fork whose hub reaches the exit only through a branch, or through
a third off-trunk fan branch, is not disqualified.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nf_metro.layout.engine import compute_layout
from nf_metro.layout.phases.fan_bundles import (
    _fork_hub_bypasses_trunk_to_exit,
    _section_has_symmetric_entry_fork,
)
from nf_metro.parser.mermaid import parse_metro_mermaid

ROOT = Path(__file__).resolve().parent.parent
RIBOSEQ = (
    ROOT
    / "tests"
    / "fixtures"
    / "curve_invariant_repros"
    / "riboseq_inter_row_corridor.mmd"
)


def _load(path: Path):
    graph = parse_metro_mermaid(path.read_text())
    compute_layout(graph)
    return graph


def _section_of(graph, station_id: str):
    section_id = graph.stations[station_id].section_id
    return graph.sections[section_id]


def test_riboseq_alignment_fork_kept_full_pitch_by_trunk_bypass():
    graph = _load(RIBOSEQ)
    alignment = _section_of(graph, "umi_dedup")

    assert _fork_hub_bypasses_trunk_to_exit(
        graph, alignment, "genomecov", "salmon_quant"
    )
    # The bypass disqualifies the fork from half-pitch compaction.  The section's
    # only other on-track column-mate pair, the producers' output files
    # bigwig_out/counts_out, is fed by two different stations, so it is not a
    # fork off one hub and does not qualify either.
    assert not _section_has_symmetric_entry_fork(graph, alignment)

    trunk = graph.stations["umi_dedup"].y
    top = graph.stations["genomecov"].y
    bottom = graph.stations["salmon_quant"].y
    # Full pitch: each branch a whole grid unit off the trunk, declared order,
    # with the trunk row left free for the bypass bundle.  The invariant is this
    # shape, not coordinate parity with the compact-then-detour path a plain
    # non-centred layout takes to reach a similar result.
    assert top < trunk < bottom
    assert (trunk - top) == pytest.approx(bottom - trunk, abs=1.0)
    assert (bottom - top) == pytest.approx(116.8, abs=2.0)
    # The full-pitch branch leaves room for its output file on-track, so the
    # Coverage sink rides genomecov's branch rather than an off-track detour.
    assert not graph.stations["bigwig_out"].off_track


@pytest.mark.parametrize(
    "fixture,branches",
    [
        ("symmetric_deadend_fanout", ("split", "salmon")),
        ("symmetric_deadend_fanout_exit", ("split", "salmon")),
        ("symmetric_join_exit_port_centre", ("ribowaltz", "plastid_psite")),
    ],
)
def test_bypass_free_forks_stay_half_pitch_eligible(fixture, branches):
    graph = _load(ROOT / "examples" / "topologies" / f"{fixture}.mmd")
    section = _section_of(graph, branches[0])
    # No hub-to-exit bypass, so the fork stays eligible and compacted.
    assert not _fork_hub_bypasses_trunk_to_exit(graph, section, *branches)
    assert _section_has_symmetric_entry_fork(graph, section)
    ya, yb = (graph.stations[b].y for b in branches)
    assert abs(ya - yb) == pytest.approx(58.4, abs=2.0)
