"""``_align_exit_ports`` re-flushes the rows its target pushes disturb.

Stage 3.4 (``_align_exit_ports``) aligns a fold section's exit port to a
target below it via ``_resolve_tb_exit_y``, which can push that target
section down -- dropping its bbox top below its contiguous row-mates'.  The
move re-flushes those rows itself, so immediately after it returns no row it
disturbed is left non-flush.

Parametrised over the corpus fixtures that exercise a bbox_y push at Stage
3.4; each case asserts the call disturbs a row (so the fixture remains a
live guard) and that every contiguous column group touching a disturbed
section is flush right after the call.
"""

from __future__ import annotations

import pytest
from conftest import content_corpus

import nf_metro.layout.engine as engine
from nf_metro.convert import convert_nextflow_dag
from nf_metro.layout.constants import SAME_COORD_TOLERANCE
from nf_metro.layout.engine import compute_layout
from nf_metro.layout.phases._common import _row_contiguous_column_groups
from nf_metro.parser import parse_metro_mermaid

# Fixtures whose fold exit-port alignment pushes a target section (changes a
# bbox_y) at Stage 3.4, so the row re-flush inside _align_exit_ports has
# something to correct.
_DISTURBING_FIXTURES = (
    "examples/variantbenchmarking",
    "guide/04_directions",
    "topologies/bt_to_tb",
    "topologies/fold_left_exit_right_entry",
)

_CORPUS = {fid: (path, is_nf) for fid, path, is_nf in content_corpus()}


@pytest.mark.parametrize("fid", _DISTURBING_FIXTURES)
def test_align_exit_ports_reflushes_disturbed_rows(fid, monkeypatch):
    path, is_nf = _CORPUS[fid]
    text = path.read_text()
    if is_nf:
        text = convert_nextflow_dag(text)
    graph = parse_metro_mermaid(text)

    real = engine._align_exit_ports
    captured: dict[str, object] = {}

    def wrapper(g):
        before = {sid: sec.bbox_y for sid, sec in g.sections.items()}
        real(g)
        disturbed = {
            sid
            for sid, sec in g.sections.items()
            if abs(sec.bbox_y - before[sid]) > SAME_COORD_TOLERANCE
        }
        worst = 0.0
        for group in _row_contiguous_column_groups(g):
            if not any(sec.id in disturbed for sec in group):
                continue
            tops = [sec.bbox_y for sec in group]
            worst = max(worst, max(tops) - min(tops))
        captured["disturbed"] = disturbed
        captured["worst"] = worst

    monkeypatch.setattr(engine, "_align_exit_ports", wrapper)
    compute_layout(graph, validate=True)

    assert captured["disturbed"], (
        f"{fid} no longer disturbs any bbox_y at Stage 3.4; this fixture no "
        f"longer guards the exit-port row re-flush -- pick another"
    )
    assert captured["worst"] <= SAME_COORD_TOLERANCE, (
        f"{fid}: after _align_exit_ports a contiguous column group it pushed "
        f"is non-flush by {captured['worst']:.1f}px; the exit-port move must "
        f"re-flush the rows it disturbs (folded-in Stage 3.5)"
    )
