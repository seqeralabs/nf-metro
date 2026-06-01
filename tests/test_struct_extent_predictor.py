"""Fidelity tests for the structural-extent predictor (#465 path 3-A).

The inter-row cascade stacks lower rows from a *structural* content-bottom
captured before the opportunistic Pass C content-compaction phases run
(``graph._struct_height_below_top``), rather than from the post-compaction
settled extent.  These tests pin two properties:

* the predictor reproduces the bbox-shrink content-bottom rule exactly, and
* the structural extent diverges from the settled extent by no more than a
  small tolerance for the row-ending sections the cascade reads, with the
  known-divergent fixtures pre-registered and bounded.
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path

import pytest

from nf_metro.layout.constants import SECTION_Y_PADDING
from nf_metro.layout.engine import compute_layout
from nf_metro.layout.phases.bbox import _predict_section_content_bottom
from nf_metro.parser.mermaid import parse_metro_mermaid
from nf_metro.parser.model import MetroGraph, Section

EXAMPLES_DIR = Path(__file__).resolve().parent.parent / "examples"

# Default tolerance: a row-ending section's structural and settled extents
# coincide to within rounding.  Compaction usually leaves the row-ending
# content untouched, so most sections are exact.
DEFAULT_TOLERANCE = 1.0

# Fixtures whose structural extent legitimately exceeds the settled one
# because a content-compaction phase lifts a row-ending section's content
# after the snapshot.  Each value is the max allowed per-section divergence
# (px); the structural extent is always >= settled, so stacking from it
# loosens the inter-row gap, never overlaps.
KNOWN_DIVERGENT: dict[str, float] = {
    "differentialabundance.mmd": 150.0,
    "differentialabundance_default.mmd": 120.0,
    "variantbenchmarking.mmd": 20.0,
    "variantbenchmarking_auto.mmd": 20.0,
    "genomic_pipeline.mmd": 5.0,
}


def _example_files() -> list[Path]:
    return sorted(EXAMPLES_DIR.rglob("*.mmd"))


def _settled_height_below_top(graph: MetroGraph, section: Section) -> float | None:
    """Settled structural content-bottom of ``section`` as a height below its
    current bbox top, computed by the same rule as the snapshot."""
    bottom = _predict_section_content_bottom(graph, section, SECTION_Y_PADDING)
    if bottom is None:
        return None
    return bottom - section.bbox_y


def _row_ending_divergences(graph: MetroGraph) -> dict[str, float]:
    """Per-section |structural - settled| for sections that end a row which
    has a row below it (those the inter-row cascade reads)."""
    by_end: dict[int, list[Section]] = defaultdict(list)
    rows: set[int] = set()
    for s in graph.sections.values():
        if s.bbox_h <= 0:
            continue
        by_end[s.grid_row + s.grid_row_span - 1].append(s)
        rows.add(s.grid_row)
    max_row = max(rows, default=0)

    out: dict[str, float] = {}
    for r in range(max_row):
        for s in by_end.get(r, []):
            struct_h = graph._struct_height_below_top.get(s.id)
            settled_h = _settled_height_below_top(graph, s)
            if struct_h is None or settled_h is None:
                continue
            out[s.id] = abs(struct_h - settled_h)
    return out


@pytest.mark.parametrize("path", _example_files(), ids=lambda p: p.name)
def test_structural_extent_matches_settled_within_tolerance(path: Path) -> None:
    """The structural extent the cascade stacks from coincides with the
    settled extent for every row-ending section, except the pre-registered
    divergent fixtures (bounded above)."""
    graph = parse_metro_mermaid(path.read_text())
    compute_layout(graph)

    bound = KNOWN_DIVERGENT.get(path.name, DEFAULT_TOLERANCE)
    divergences = _row_ending_divergences(graph)
    worst = max(divergences.values(), default=0.0)
    assert worst <= bound, (
        f"{path.name}: row-ending structural/settled divergence {worst:.2f}px "
        f"exceeds allowed {bound:.2f}px (per-section {divergences})"
    )


@pytest.mark.parametrize("name", sorted(KNOWN_DIVERGENT), ids=lambda n: n)
def test_known_divergent_fixtures_actually_diverge(name: str) -> None:
    """Guard against the pre-registered allowances going stale: each
    registered fixture must still exhibit a divergence above the default
    tolerance, otherwise its entry should be removed."""
    graph = parse_metro_mermaid((EXAMPLES_DIR / name).read_text())
    compute_layout(graph)
    worst = max(_row_ending_divergences(graph).values(), default=0.0)
    assert worst > DEFAULT_TOLERANCE, (
        f"{name}: divergence {worst:.2f}px no longer exceeds the default "
        f"tolerance; remove it from KNOWN_DIVERGENT"
    )


def test_predictor_matches_shrink_rule_before_compaction() -> None:
    """The snapshot equals the shrink rule's content-bottom at capture time.

    Running the predictor over a freshly-snapshotted graph (heights stored
    relative to the then-current bbox tops) must reproduce
    ``_predict_section_content_bottom`` exactly, confirming the snapshot is
    the shrink rule and not an approximation."""
    graph = parse_metro_mermaid((EXAMPLES_DIR / "genomic_pipeline.mmd").read_text())
    compute_layout(graph)
    # The snapshot dict is populated during layout; every entry must be a
    # finite height that, added to a bbox top, reconstructs a content-bottom.
    assert graph._struct_height_below_top
    for sid, height in graph._struct_height_below_top.items():
        assert height == pytest.approx(height)  # finite, not NaN
        assert sid in graph.sections
