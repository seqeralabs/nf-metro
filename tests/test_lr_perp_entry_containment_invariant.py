"""Containment invariant for LR/RL sections with a perpendicular entry.

When a section's TOP/BOTTOM entry port is aligned to an upstream drop that
lands outside the run's natural column span (a "cross-column" drop), Stage
3.3 shifts the run sideways to sit under the drop.  The section bbox must
follow that shift so every internal station stays inside it; a fixed
pre-reserved inset under-sizes the bbox and leaves the trailing station
outside (issue #1057).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from layout_validator import Severity, check_station_containment

from nf_metro.layout.engine import compute_layout
from nf_metro.parser.mermaid import parse_metro_mermaid

TOPOLOGIES_DIR = Path(__file__).parent.parent / "examples" / "topologies"

# Fixtures whose LR/RL sections take a perpendicular (TOP/BOTTOM) entry and
# undergo the Stage 3.3 entry shift.  lr_top_entry_cross_column is the #1057
# cross-column repro; the rest guard the same-column and diverging variants
# so the bbox reconciliation generalises rather than special-casing one graph.
PERP_ENTRY_FIXTURES = [
    "lr_top_entry_cross_column",
    "cross_col_top_entry",
    "lr_perp_bottom_exit_perp_entry",
    "lr_perp_top_exit_perp_entry",
    "lr_perp_top_exit_perp_entry_diverging",
    "merge_trunk_out_of_range_section",
    "rl_left_exit_top_entry_overflow",
]


@pytest.mark.parametrize("stem", PERP_ENTRY_FIXTURES)
def test_perp_entry_run_stays_contained(stem: str) -> None:
    graph = parse_metro_mermaid((TOPOLOGIES_DIR / f"{stem}.mmd").read_text())
    compute_layout(graph, validate=True)

    errors = [
        v for v in check_station_containment(graph) if v.severity == Severity.ERROR
    ]
    assert not errors, "\n".join(v.message for v in errors)
