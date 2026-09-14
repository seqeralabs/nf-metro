"""Content-hugging section bboxes are the default; row-top flush is opt-in.

Under the ``row_align: content`` default each section's bbox hugs its own
content; ``%%metro row_align: top`` instead grows shorter row-mates upward so
a packed grid-cell row's bbox tops (and header badges) sit flush.

``examples/riboseq_metro.mmd`` exercises both: its bottom grid row packs a
six-way fan (``orf_calling``) beside a short ``psite_id`` and a
single-station ``reporting`` box.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nf_metro.layout.constants import SECTION_Y_PADDING
from nf_metro.layout.engine import compute_layout
from nf_metro.parser.mermaid import parse_metro_mermaid

EXAMPLES_DIR = Path(__file__).resolve().parent.parent / "examples"
FIXTURE = EXAMPLES_DIR / "riboseq_metro.mmd"


def _riboseq_graph(row_align: str | None = None):
    graph = parse_metro_mermaid(FIXTURE.read_text())
    if row_align is not None:
        graph.row_align = row_align
    compute_layout(graph)
    return graph


def _top_pad(graph, section_id: str) -> float:
    section = graph.sections[section_id]
    stations = [
        st
        for st in graph.stations.values()
        if st.section_id == section_id and not st.is_port
    ]
    top = min(st.y for st in stations)
    return top - section.bbox_y


@pytest.mark.parametrize("section_id", ["reporting", "psite_id"])
def test_short_packed_rowmate_hugs_content_by_default(section_id):
    """A short packed-row section hugs its content top under the default.

    Forced row-top alignment inflates the top padding to roughly one grid
    unit of dead space above the section's own content; the content default
    keeps it at the padding convention.
    """
    graph = _riboseq_graph()
    assert graph.row_align == "content"
    top_pad = _top_pad(graph, section_id)
    assert top_pad == pytest.approx(SECTION_Y_PADDING, abs=5.0), (
        f"{section_id}: top padding {top_pad:.1f} is not a content-hug "
        f"(expected ~{SECTION_Y_PADDING:.0f}); the box is padded up to a "
        f"taller row-mate instead of hugging its own content"
    )


def test_row_align_top_opt_in_flushes_row_tops():
    """``row_align: top`` levels a packed row's bbox tops."""
    graph = _riboseq_graph(row_align="top")
    row1_tops = [
        sec.bbox_y
        for sec in graph.sections.values()
        if sec.grid_row == 1 and sec.bbox_h > 0
    ]
    assert len(row1_tops) >= 2
    assert max(row1_tops) - min(row1_tops) < 2.0, (
        f"row_align: top must level packed-row bbox tops, got {row1_tops}"
    )


def test_content_default_lowers_short_rowmate_below_fan():
    """The content default lets a short box sit below its taller row-mate.

    The opt-in ``top`` mode pins the two together; confirming they diverge
    under the default guards against a silent revert to forced alignment.
    """
    content = _riboseq_graph()
    top = _riboseq_graph(row_align="top")
    assert content.sections["reporting"].bbox_y > top.sections["reporting"].bbox_y + 2.0
