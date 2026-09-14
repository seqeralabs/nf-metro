"""A section's bbox bottom is released unless a row-mate shares that bottom.

Stage 6.13 shrinks each section's bbox down onto its content, but declines to
when a grid row-mate's bottom pins it.  The pin exists for sections that are
*bottom-aligned* with a mate (a TB fold grown to span into the next row, a
Stage 6.5 aligned pair, a rowspan sidebar): dropping their bottom would break a
deliberately shared bottom edge.  A row-mate that merely happens to be deeper
carries no such intent, and must not hold a section's bottom above its content.

The riboseq map has both in one grid row: ``orf_calling`` is much the deepest
member and shares its bottom with nobody, so ``te`` and ``psite_id`` must hug
their own content.  The preservation fixtures cover the three shapes where the
pin is meant to fire.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from riboseq_map import RIBOSEQ_MMD

from nf_metro.layout.constants import SAME_COORD_TOLERANCE, SECTION_Y_PADDING
from nf_metro.layout.engine import compute_layout
from nf_metro.layout.phases.bbox import _predict_section_content_bottom
from nf_metro.layout.routing import compute_station_offsets
from nf_metro.parser.mermaid import parse_metro_mermaid

_EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
_TOL = 1.0


def _layout(text: str):
    graph = parse_metro_mermaid(text)
    compute_layout(graph, validate=False)
    return graph


def _bbox_bottom(graph, section_id: str) -> float:
    section = graph.sections[section_id]
    return section.bbox_y + section.bbox_h


def _content_excess(graph, section_id: str) -> float:
    """Px of bbox below the section's content-hug target."""
    section = graph.sections[section_id]
    hug = _predict_section_content_bottom(
        graph, section, SECTION_Y_PADDING, compute_station_offsets(graph)
    )
    assert hug is not None, section_id
    return _bbox_bottom(graph, section_id) - hug


@pytest.fixture(scope="module")
def riboseq():
    return _layout(RIBOSEQ_MMD)


@pytest.mark.parametrize("section_id", ["te", "psite_id"])
def test_shallow_row_mate_hugs_its_own_content(riboseq, section_id):
    # te's terminus is lifted onto the join centreline and psite_id's stack is
    # short; neither shares a bottom with anything, so the deepest row-mate
    # (orf_calling) must not reserve a dead band beneath them.
    assert _content_excess(riboseq, section_id) <= _TOL


@pytest.mark.parametrize("section_id", ["orf_calling", "reporting"])
def test_unpinned_row_mates_keep_hugging_content(riboseq, section_id):
    assert _content_excess(riboseq, section_id) <= _TOL


def test_row_bottoms_stay_independent(riboseq):
    # The row is not a shared bottom band: four members, four bottoms.
    bottoms = [
        _bbox_bottom(riboseq, sid)
        for sid in ("orf_calling", "psite_id", "te", "reporting")
    ]
    assert len(set(round(b, 1) for b in bottoms)) == 4, bottoms


@pytest.mark.parametrize(
    "fixture,section_id,mate_id",
    [
        # TB fold grown by section_y_gap to reach its target's bottom.
        ("topologies/fold_left_exit_right_entry", "middle", "report"),
        # LR pair bottom-aligned within one grid row.
        ("guide/05f_banner_labels", "analysis", "reporting"),
        # Rowspan sidebar sharing the bottom of the row it spans into.
        ("topologies/multirow_source_stacked_fan", "align_sec", "qc_sec"),
    ],
)
def test_bottom_aligned_mate_still_pins(fixture, section_id, mate_id):
    graph = _layout((_EXAMPLES / f"{fixture}.mmd").read_text())
    section_bot = _bbox_bottom(graph, section_id)
    # The pin is load-bearing here: without it the section would drop onto its
    # own content, tearing the shared bottom edge.
    assert _content_excess(graph, section_id) > _TOL
    assert abs(section_bot - _bbox_bottom(graph, mate_id)) <= SAME_COORD_TOLERANCE
