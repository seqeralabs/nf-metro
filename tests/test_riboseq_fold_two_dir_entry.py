"""Two-direction entry into a folded return-row section (#1341 / #1342).

A folded Ribo-seq excerpt: ``psite_id`` sits on the RL return row, fed from
``quantification`` directly above it and ``orf_calling`` to its right.

The hint-free variant is the goal state -- entry-side inference (#1342) picks a
single sensible side from the feed geometry and the map lays out cleanly.  The
hand-tuned variant (``riboseq_fold_two_dir_entry.mmd``) pins the grid and forces
``entry: top``; it renders but exhibits the route-around and overlap defects
tracked by #1343 (exit-port re-pin), #1344 (overlap-free placement) and
#1341 goal 2 (off-side route-around), caught by the corpus invariant suite until
those land.
"""

from __future__ import annotations

import sys
import warnings
from pathlib import Path

from nf_metro.layout.engine import compute_layout
from nf_metro.parser.mermaid import parse_metro_mermaid
from nf_metro.parser.model import MetroGraph, PortSide

sys.path.insert(0, str(Path(__file__).parent))
from layout_validator import check_section_overlap  # noqa: E402

HINTLESS = "examples/topologies/riboseq_fold_two_dir_entry_hintless.mmd"


def _layout(path: str, *, validate: bool, strict: bool = False) -> MetroGraph:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        graph = parse_metro_mermaid(open(path).read())
    graph.strict = strict
    compute_layout(graph, validate=validate)
    return graph


def test_hintless_lays_out_under_strict() -> None:
    """The hint-free variant lays out without raising, even under strict."""
    _layout(HINTLESS, validate=True, strict=True)


def test_hintless_psite_infers_single_sensible_entry_side() -> None:
    """psite_id (fed from above + right) infers one entry side, the fed RIGHT.

    With no ``entry:`` hint the return row resolves to RL and the entry side is
    inferred from the feed geometry: RIGHT is both the flow-natural side and a
    side a feed reaches, so it is chosen and shared by every entering line.
    """
    graph = _layout(HINTLESS, validate=False)
    sides = {graph.ports[pid].side for pid in graph.sections["psite_id"].entry_ports}
    assert sides == {PortSide.RIGHT}


def test_hintless_no_section_overlap() -> None:
    """The hint-free variant places every section box clear of its neighbours."""
    graph = _layout(HINTLESS, validate=False)
    overlaps = check_section_overlap(graph)
    assert not overlaps, "\n".join(v.message for v in overlaps)
