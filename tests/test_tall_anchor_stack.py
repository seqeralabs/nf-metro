"""Tall-anchor vertical-stack placement.

A pipeline whose section meta-graph is a single-source/single-sink chain
containing one section that is much taller than it is wide (a large fan,
e.g. a 19-way variant-caller block) should be packed by stacking the narrow
downstream chain vertically beside the tall anchor, rather than spreading
every section into its own topological column. The latter sprawls the canvas
horizontally; the former keeps it compact.
"""

import re
from pathlib import Path

import pytest

from nf_metro.layout.auto_layout import _detect_tall_anchor_chain
from nf_metro.layout.engine import compute_layout
from nf_metro.layout.phases.guards import (
    PhaseInvariantError,
    _guard_tall_anchor_stack_well_formed,
)
from nf_metro.parser.mermaid import parse_metro_mermaid
from nf_metro.render.svg import render_svg
from nf_metro.themes import NFCORE_THEME

EXAMPLES = Path(__file__).parent.parent / "examples"


def _strip_grid_pins(path: Path) -> str:
    """Return the example text with all explicit ``grid:`` pins removed."""
    return "\n".join(
        line
        for line in path.read_text().splitlines()
        if not line.startswith("%%metro grid:")
    )


def _section_columns(graph) -> set[int]:
    """Grid columns occupied by any section (accounting for col spans)."""
    cols: set[int] = set()
    for section in graph.sections.values():
        if section.grid_col < 0:
            continue
        for col in range(
            section.grid_col, section.grid_col + section.grid_col_span
        ):
            cols.add(col)
    return cols


def _render_width(text: str) -> int:
    graph = parse_metro_mermaid(text)
    compute_layout(graph)
    svg = render_svg(graph, NFCORE_THEME)
    match = re.search(r'width="(\d+)"', svg)
    assert match is not None
    return int(match.group(1))


def test_tall_anchor_chain_stacks_into_two_columns():
    """genomic_pipeline (grid pins stripped) packs into <= 2 section columns."""
    graph = parse_metro_mermaid(_strip_grid_pins(EXAMPLES / "genomic_pipeline.mmd"))
    cols = _section_columns(graph)
    assert cols, "no sections placed"
    assert max(cols) <= 1, (
        f"expected the tall-anchor chain to stack into <= 2 columns, "
        f"got columns {sorted(cols)}"
    )


def test_tall_anchor_auto_matches_or_beats_pinned_width():
    """Auto layout is no wider than the hand-pinned grid (issue acceptance)."""
    pinned = _render_width((EXAMPLES / "genomic_pipeline.mmd").read_text())
    auto = _render_width(_strip_grid_pins(EXAMPLES / "genomic_pipeline.mmd"))
    assert auto <= pinned * 1.05, (
        f"auto width {auto}px should match or beat pinned width {pinned}px"
    )


def test_tall_anchor_detected_on_genomic_pipeline():
    """The dominant tall-narrow caller fan is identified as the anchor."""
    graph = parse_metro_mermaid(_strip_grid_pins(EXAMPLES / "genomic_pipeline.mmd"))
    assert _detect_tall_anchor_chain(graph) == "variant_calling"


# Multi-section auto examples lacking a dominant tall-narrow anchor: the packer
# must not fire (each tallest section is short, wide, or the graph branches /
# has multiple sinks), so the detector returns None and their layout is
# governed by the ordinary topological packer.
NON_FIRING = [
    "epitopeprediction.mmd",
    "hlatyping.mmd",
    "variant_calling.mmd",
    "variant_calling_tuned.mmd",
    "genomeassembly.mmd",
    "longread_variant_calling.mmd",
    "differentialabundance.mmd",
    "variantbenchmarking.mmd",
    "rnaseq_auto.mmd",
]


@pytest.mark.parametrize("name", NON_FIRING)
def test_tall_anchor_does_not_fire_on_ordinary_pipelines(name):
    graph = parse_metro_mermaid(_strip_grid_pins(EXAMPLES / name))
    assert _detect_tall_anchor_chain(graph) is None, (
        f"{name}: tall-anchor packer fired but should not"
    )


def test_guard_rejects_reoriented_tail_section():
    """A stacked tail section flipped to vertical flow trips the guard."""
    graph = parse_metro_mermaid(_strip_grid_pins(EXAMPLES / "genomic_pipeline.mmd"))
    graph.sections["annotation"].direction = "TB"
    with pytest.raises(PhaseInvariantError, match="not horizontal-flow"):
        _guard_tall_anchor_stack_well_formed(graph, "test")


def test_guard_rejects_tail_section_off_anchor_span():
    """A tail section dropped below the anchor's row span trips the guard."""
    graph = parse_metro_mermaid(_strip_grid_pins(EXAMPLES / "genomic_pipeline.mmd"))
    anchor = graph.sections["variant_calling"]
    bottom = anchor.grid_row + anchor.grid_row_span - 1
    graph.sections["reporting"].grid_row = bottom + 5
    with pytest.raises(PhaseInvariantError, match="outside the anchor"):
        _guard_tall_anchor_stack_well_formed(graph, "test")
