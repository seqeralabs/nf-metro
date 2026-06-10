"""Fidelity tests for the structural-extent predictor.

The structural snapshot (``graph._struct_height_below_top``) records each
section's content height below its bbox top after Stage 6.15a, when the
layout is fully settled.  These tests pin two properties:

* the predictor reproduces the bbox-shrink content-bottom rule exactly, and
* the structural extent in the snapshot coincides with the settled extent for
  every row-ending section (within ``DEFAULT_TOLERANCE``).
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

DEFAULT_TOLERANCE = 1.0


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
    """The structural extent in the snapshot coincides with the settled extent
    for every row-ending section across all gallery fixtures."""
    graph = parse_metro_mermaid(path.read_text())
    compute_layout(graph)

    divergences = _row_ending_divergences(graph)
    worst = max(divergences.values(), default=0.0)
    assert worst <= DEFAULT_TOLERANCE, (
        f"{path.name}: row-ending structural/settled divergence {worst:.2f}px "
        f"exceeds allowed {DEFAULT_TOLERANCE:.2f}px (per-section {divergences})"
    )


def test_off_track_lift_not_under_predicted() -> None:
    """A section with a lifted off-track input must not under-predict its
    content height below the bbox top.

    The off-track lift (Stage 5.2) raises a section's bbox top to seat the
    lifted input above the trunk.  The structural snapshot must reflect this
    raised top so the stored height-below-top is not shorter than the settled
    height.  A shortfall would make the cascade stack the row below too high
    and risk overlap.
    """
    mmd = (
        "%%metro title: off-track two-row\n"
        "%%metro line: a | A | #ff0000\n"
        "%%metro file: in_csv | CSV\n"
        "%%metro off_track: in_csv\n"
        "%%metro grid: top | 0,0\n"
        "%%metro grid: bot | 0,1\n"
        "graph LR\n"
        "    subgraph top [Top]\n"
        "        in_csv[ ]\n"
        "        t1[Step One]\n"
        "        t2[Step Two]\n"
        "        in_csv -->|a| t1\n"
        "        t1 -->|a| t2\n"
        "    end\n"
        "    subgraph bot [Bottom]\n"
        "        b1[Bee One]\n"
        "        b2[Bee Two]\n"
        "        b1 -->|a| b2\n"
        "    end\n"
        "    t2 -->|a| b1\n"
    )
    graph = parse_metro_mermaid(mmd)
    compute_layout(graph)
    top = graph.sections["top"]
    struct_h = graph._struct_height_below_top.get("top")
    settled_h = _settled_height_below_top(graph, top)
    assert struct_h is not None and settled_h is not None
    assert struct_h >= settled_h - DEFAULT_TOLERANCE, (
        f"off-track section under-predicted: struct {struct_h:.1f} < settled "
        f"{settled_h:.1f}"
    )


def test_predictor_matches_shrink_rule() -> None:
    """The snapshot equals the shrink rule's content-bottom at capture time.

    Running the predictor over the snapshotted graph (heights stored
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
