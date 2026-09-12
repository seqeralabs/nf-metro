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
]


@pytest.mark.parametrize("stem", PERP_ENTRY_FIXTURES)
def test_perp_entry_run_stays_contained(stem: str) -> None:
    graph = parse_metro_mermaid((TOPOLOGIES_DIR / f"{stem}.mmd").read_text())
    compute_layout(graph, validate=True)

    errors = [
        v for v in check_station_containment(graph) if v.severity == Severity.ERROR
    ]
    assert not errors, "\n".join(v.message for v in errors)


# A RL section whose perp-entry drop is *predicted* to land right of the run
# (perp_entry_lands_left() is False) but whose actual entry port sits left of
# it: the run shifts left to meet the port and, without a matching bbox grow,
# escapes into the neighbouring column.  validate=True is not used here because
# an unrelated perp-entry offset-swap defect trips a routing guard on one line;
# the containment invariant this test locks is checked directly.
_RIGHT_PREDICTED_OVERFLOW_MMD = """\
%%metro title: Left-exit fan riser
%%metro line: g | Green | #2db572
%%metro line: h | Blue | #3f7fdf
%%metro grid: a | 1,0
%%metro grid: b | 1,1
%%metro grid: c | 1,2
%%metro grid: d | 0,1

graph LR
    subgraph a [Alpha]
        %%metro exit: left | g, h
        a1[A one]
        a2[A two]
        a1 -->|g,h| a2
    end
    subgraph b [Beta]
        %%metro entry: top | g, h
        %%metro exit: left | g, h
        b1[B one]
        b2[B two]
        b1 -->|g,h| b2
    end
    subgraph c [Gamma]
        %%metro entry: top | g
        c1[C one]
        c2[C two]
        c1 -->|g| c2
    end
    subgraph d [Delta]
        %%metro entry: right | h
        d1[D one]
        d2[D two]
        d1 -->|h| d2
    end
    a2 -->|g,h| b1
    b2 -->|g| c1
    b2 -->|h| d1
"""


def test_right_predicted_perp_entry_overflow_grows_bbox() -> None:
    graph = parse_metro_mermaid(_RIGHT_PREDICTED_OVERFLOW_MMD)
    compute_layout(graph, validate=False)

    errors = [
        v for v in check_station_containment(graph) if v.severity == Severity.ERROR
    ]
    assert not errors, "\n".join(v.message for v in errors)
