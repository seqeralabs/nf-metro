"""Invariant: single-row sections stacked beside a rowspan neighbour fill its band.

When a column holds single-row sections stacked one per grid row beside a taller
``grid_row_span > 1`` section spanning those same rows, the stack must be
distributed across that section's vertical band: the topmost section's bbox top
meets the band top and the bottommost's bbox bottom meets the band bottom.

Without this the topmost section's fan, centred on its row line, spreads upward
out of the layout into the title band, and the bottommost section floats high
with empty slack beneath it.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from layout_validator import Severity, validate_layout

from nf_metro.layout.constants import SAME_COORD_TOLERANCE
from nf_metro.layout.engine import compute_layout
from nf_metro.layout.phases._common import iter_stacked_rows_in_rowspan_band
from nf_metro.parser.mermaid import parse_metro_mermaid

EXAMPLES_DIR = Path(__file__).parent.parent / "examples"
SHOWCASE_DIR = EXAMPLES_DIR / "showcase"

# Fixtures with a single-row section stack beside a rowspan neighbour.
FIXTURES = [
    "single_row_rowspan_neighbor",
]


@pytest.mark.parametrize("stem", FIXTURES)
def test_stacked_rows_fill_rowspan_band(stem: str) -> None:
    graph = parse_metro_mermaid((SHOWCASE_DIR / f"{stem}.mmd").read_text())
    compute_layout(graph, validate=True)

    stacks = list(iter_stacked_rows_in_rowspan_band(graph, SAME_COORD_TOLERANCE))
    assert stacks, (
        f"{stem}: expected a single-row stack beside a rowspan neighbour; "
        "fixture no longer exercises the invariant"
    )

    for stack, band_top, band_bot in stacks:
        top = stack[0]
        bot = stack[-1]
        assert abs(top.bbox_y - band_top) <= SAME_COORD_TOLERANCE, (
            f"{stem}: top section '{top.id}' bbox top {top.bbox_y:.1f} does not "
            f"meet band top {band_top:.1f} (rises out of the band by "
            f"{band_top - top.bbox_y:.1f}px)"
        )
        bot_edge = bot.bbox_y + bot.bbox_h
        assert abs(bot_edge - band_bot) <= SAME_COORD_TOLERANCE, (
            f"{stem}: bottom section '{bot.id}' bbox bottom {bot_edge:.1f} does "
            f"not meet band bottom {band_bot:.1f} (slack of "
            f"{band_bot - bot_edge:.1f}px below it)"
        )


@pytest.mark.parametrize(
    "rel_path",
    [
        "showcase/single_row_rowspan_neighbor.mmd",
        "topologies/internal_source_equal_sibling_2fan.mmd",
    ],
)
def test_internal_source_2fan_source_centred(rel_path: str) -> None:
    """An internal source feeding two equal-sibling branches is centred (#1223).

    In both fixtures the run_folder line's source icon ``rundir_in`` fans to
    ``checkqc`` and ``rundirparser``: two equal-sibling branches carrying the
    same single line into the same downstream join.  Neither branch is a
    designated trunk continuation, so the source sits centred between the two
    consumers (collinear with the centred trunk and the symmetric exit bundle)
    rather than on one branch's lane while the other peels off.  The centred
    trunk Y is the half-grid symfan frame the source shares; it is also clean
    (an integer grid multiple), keeping the source off the rowspan neighbour's
    fractional grid origin.
    """
    graph = parse_metro_mermaid((EXAMPLES_DIR / rel_path).read_text())
    compute_layout(graph, validate=True)

    hub = graph.stations["rundir_in"]
    lo = graph.stations["checkqc"]
    hi = graph.stations["rundirparser"]
    midpoint = (lo.y + hi.y) / 2.0
    assert abs(hub.y - midpoint) <= SAME_COORD_TOLERANCE, (
        f"{rel_path}: source hub 'rundir_in' (y={hub.y:.3f}) is not centred "
        f"between 'checkqc' (y={lo.y:.3f}) and 'rundirparser' (y={hi.y:.3f}); "
        f"midpoint={midpoint:.3f}, off by {abs(hub.y - midpoint):.3f}px"
    )


@pytest.mark.parametrize("stem", FIXTURES)
def test_showcase_fixture_has_no_layout_errors(stem: str) -> None:
    """The relocated fixture skips the auto-globbed topology corpus, so run the
    error-level layout validation here (sub-pixel warnings from the center-ported
    fan are out of scope)."""
    graph = parse_metro_mermaid((SHOWCASE_DIR / f"{stem}.mmd").read_text())
    compute_layout(graph, validate=True)

    errors = [v for v in validate_layout(graph) if v.severity == Severity.ERROR]
    assert not errors, "\n".join(v.message for v in errors)
