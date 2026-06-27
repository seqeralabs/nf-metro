"""Corpus oracle for the seam-orientation classifier (issue #1040).

:func:`seam_orientation` is a pure, seam-local primitive: given a feeding exit
port and the entry port it feeds, it returns whether the bundle order arrives
*preserved* or *reversed*, derived only from port sides and grid topology. These
tests pin it against the legacy reversal machinery across every inter-section
seam in the corpus.

Two guarantees:

* **Soundness** -- the classifier never reverses a seam the machinery keeps. Every
  transposition it reports corresponds to a real reversal idiom.
* **Documented residuals** -- the machinery additionally marks a fixed set of
  seams reversed that the classifier preserves. These are *not* classifier
  failures: in every case the transposition was introduced at an upstream seam
  and rides through in the delivered bundle order, while the legacy
  section-absolute flag (``detect_reversed_sections`` row-propagation and the
  ``_is_tb_lr_exit_nonreversed`` "not itself already reversed" clause) re-reports
  it per section. The lane-order landing (#1041) resolves these by driving order
  from the arrival bundle rather than re-reversing at the seam.
"""

from __future__ import annotations

import warnings
from functools import lru_cache
from pathlib import Path

import pytest

from nf_metro.layout.constants import COORD_TOLERANCE
from nf_metro.layout.engine import compute_layout
from nf_metro.layout.routing.context import (
    is_far_side_around_below_left_entry,
    is_near_vertical_junction_right_entry,
)
from nf_metro.layout.routing.reversal import detect_reversed_sections
from nf_metro.layout.routing.seam import SeamOrientation, seam_orientation
from nf_metro.parser.mermaid import parse_metro_mermaid
from nf_metro.parser.model import MetroGraph, Port, PortSide


def _machinery_is_over_top_right_entry(
    graph: MetroGraph, port: Port, tb_sections: set[str]
) -> bool:
    """Whether *port* is a RIGHT entry reached by an over-the-top loop.

    Ground-truth detection for the over-the-top RIGHT-entry idiom, against which
    this corpus oracle checks :func:`seam_orientation`.  A RIGHT entry on a TB
    section fed by an exit port in the SAME grid row, an ADJACENT column, and to
    the port's LEFT: that feed loops over the section's top and approaches from
    the right -- a U-turn that transposes the bundle.  A right entry fed from the
    right (a fold) or across columns (a bypass) keeps its order and is excluded.
    """
    if not (port.is_entry and port.side == PortSide.RIGHT):
        return False
    if port.section_id not in tb_sections:
        return False
    psec = graph.sections.get(port.section_id)
    pst = graph.stations.get(port.id)
    if psec is None or pst is None:
        return False
    for edge in graph.edges_to(port.id):
        src = graph.stations.get(edge.source)
        src_port = graph.ports.get(edge.source)
        if not (src and src_port and not src_port.is_entry):
            continue
        ssec = graph.sections.get(src.section_id) if src.section_id else None
        if ssec is None:
            continue
        if (
            ssec.grid_row == psec.grid_row
            and abs(ssec.grid_col - psec.grid_col) <= 1
            and src.x < pst.x - COORD_TOLERANCE
        ):
            return True
    return False


EXAMPLES_DIR = Path(__file__).parent.parent / "examples"
TOPOLOGIES_DIR = EXAMPLES_DIR / "topologies"

CORPUS_FILES = sorted(EXAMPLES_DIR.glob("*.mmd")) + sorted(TOPOLOGIES_DIR.glob("*.mmd"))
CORPUS_IDS = [
    f"{f.parent.name}/{f.stem}" if f.parent.name == "topologies" else f.stem
    for f in CORPUS_FILES
]

# Seams the legacy machinery marks reversed but the seam-local classifier
# preserves. Keyed (fixture, feeder section, consumer section,
# exit-side -> entry-side). Two families, both documented in seam.py:
#   - propagated / conditional reversal carried in the delivered bundle from an
#     upstream fold turn (the bulk), and
#   - the near-vertical junction RIGHT entry, whose reversal turns on pixel
#     overhang rather than sides/grid and is deferred coordinate-free.
NEAR_VERTICAL_RESIDUAL = ("near_vertical_junction_hook", "src", "pseudo", "R->R")
EXPECTED_RESIDUALS = frozenset(
    {
        NEAR_VERTICAL_RESIDUAL,
        # positive_fan TB (right-entry) feeding a vertical TB column below it:
        # the machinery marks the lower section as positive_fan so it draws on
        # the same +x side as the drop, but the classifier sees a vertical→vertical
        # continuation and correctly says PRESERVE (no bundle-order flip).
        ("tb_right_entry_stack", "upper", "lower", "B->T"),
        # left_exit_sink_below: a TB bridge's LEFT exit drops into a LEFT-entry
        # sink below and to the left.  The section-absolute flag marks the sink
        # reversed, but the seam is a single-line L->L drop with no bundle order
        # to flip, so the classifier preserves it.
        ("left_exit_sink_below", "bridge", "sink", "L->L"),
        ("fold_double", "annotation", "interpretation", "L->R"),
        ("fold_double", "hard_filter", "annotation", "L->R"),
        ("fold_double", "interpretation", "integration", "L->R"),
        ("fold_fan_across", "stat_analysis", "reporting", "L->R"),
        ("fold_stacked_branch", "bio_interp", "final_report", "L->R"),
        ("longread_variant_calling", "annotation", "reports", "L->R"),
        ("longread_variant_calling", "cnv_calling", "reports", "L->R"),
        ("longread_variant_calling", "jointcalling", "annotation", "L->R"),
        ("longread_variant_calling", "small_variants", "annotation", "R->R"),
        ("longread_variant_calling", "small_variants", "jointcalling", "R->R"),
        ("longread_variant_calling", "tr_calling", "reports", "L->R"),
        ("reconverge_reversed_fold", "bio_interp", "final_report", "L->R"),
        ("u_turn_fold", "sec7", "sec8", "L->R"),
        ("variantbenchmarking", "benchmarking", "output_processing", "L->R"),
        ("variantbenchmarking", "ensembl_truth", "benchmarking", "L->R"),
        ("variantbenchmarking", "filtering", "benchmarking", "R->R"),
        ("variantbenchmarking", "normalization", "benchmarking", "R->R"),
    }
)


def _machinery_reverses(graph, entry_port, sec_id, tb_sections, reversed_secs) -> bool:
    """The reversal the scattered legacy machinery effectively applies to a seam."""
    return (
        sec_id in reversed_secs
        or _machinery_is_over_top_right_entry(graph, entry_port, tb_sections)
        or is_far_side_around_below_left_entry(graph, entry_port)
        or is_near_vertical_junction_right_entry(graph, entry_port)
    )


def _side_pair(exit_port, entry_port) -> str:
    """Seam side signature, e.g. ``"R->L"`` for a RIGHT exit into a LEFT entry."""
    return f"{exit_port.side.name[0]}->{entry_port.side.name[0]}"


def _resolve_exit_ports(graph, entry_port_id):
    """Feeding exit port(s) for an entry, resolved through any fold/merge junction."""
    junction_ids = graph.junction_ids
    exits = []
    for edge in graph.edges_to(entry_port_id):
        if edge.source in junction_ids:
            for upstream in graph.edges_to(edge.source):
                src_port = graph.ports.get(upstream.source)
                if src_port is not None and not src_port.is_entry:
                    exits.append(src_port)
        else:
            src_port = graph.ports.get(edge.source)
            if src_port is not None and not src_port.is_entry:
                exits.append(src_port)
    return exits


@lru_cache(maxsize=None)
def _seam_verdicts(path_str: str):
    """All inter-section seams in a fixture with classifier + machinery verdicts.

    Returns a list of ``(signature, classifier_reverse, machinery_reverse)``.
    ``compute_layout`` is the only layout pass run -- proving the classifier needs
    no port-offset state (those are computed later, in ``compute_station_offsets``).
    """
    path = Path(path_str)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        graph = parse_metro_mermaid(path.read_text(), max_station_columns=15)
        compute_layout(graph)
    tb_sections = {sid for sid, s in graph.sections.items() if s.direction == "TB"}
    reversed_secs = detect_reversed_sections(graph)
    rows = []
    for sec_id, section in graph.sections.items():
        for ep_id in section.entry_ports:
            entry = graph.ports.get(ep_id)
            if entry is None:
                continue
            machinery = _machinery_reverses(
                graph, entry, sec_id, tb_sections, reversed_secs
            )
            for exit_port in _resolve_exit_ports(graph, ep_id):
                sig = (
                    path.stem,
                    exit_port.section_id,
                    sec_id,
                    _side_pair(exit_port, entry),
                )
                classifier = (
                    seam_orientation(graph, exit_port, entry) is SeamOrientation.REVERSE
                )
                rows.append((sig, classifier, machinery))
    return rows


@pytest.mark.parametrize("path", CORPUS_FILES, ids=CORPUS_IDS)
def test_classifier_never_reverses_a_kept_seam(path: Path) -> None:
    """Soundness: every seam the classifier reverses, the machinery reverses too."""
    false_reverses = [
        sig
        for sig, classifier, machinery in _seam_verdicts(str(path))
        if classifier and not machinery
    ]
    assert not false_reverses, (
        f"{path.stem}: classifier reversed seam(s) the machinery keeps: "
        f"{false_reverses}"
    )


def test_residual_set_matches_documented_divergences() -> None:
    """The machinery-reverses / classifier-preserves gap is exactly the pinned set."""
    residuals = set()
    for path in CORPUS_FILES:
        for sig, classifier, machinery in _seam_verdicts(str(path)):
            if machinery and not classifier:
                residuals.add(sig)
    assert residuals == EXPECTED_RESIDUALS, {
        "unexpected (new divergence)": residuals - EXPECTED_RESIDUALS,
        "missing (now matched -- prune from list)": EXPECTED_RESIDUALS - residuals,
    }


def test_classifier_agrees_on_the_bulk_of_reversals() -> None:
    """The classifier reproduces most reversals (sanity floor on coverage)."""
    agree = under = 0
    for path in CORPUS_FILES:
        for _sig, classifier, machinery in _seam_verdicts(str(path)):
            if machinery and classifier:
                agree += 1
            elif machinery and not classifier:
                under += 1
    assert agree > under, f"coverage too low: agree={agree} residual={under}"


# --- Each reversing idiom must actually fire on a representative fixture ---


def _verdict(stem: str, feeder: str, consumer: str):
    path = next(p for p in CORPUS_FILES if p.stem == stem)
    for sig, classifier, _machinery in _seam_verdicts(str(path)):
        if sig[1] == feeder and sig[2] == consumer:
            return classifier
    raise AssertionError(f"no {feeder}->{consumer} seam in {stem}")


@pytest.mark.parametrize(
    ("stem", "feeder", "consumer"),
    [
        ("tb_right_entry_stack", "source", "upper"),  # over-the-top RIGHT entry
        ("bypass_leftward_far_side_entry", "src_sec", "tgt_sec"),  # around-below LEFT
        ("rnaseq_sections", "postprocessing", "qc_report"),  # TB column continuation
        ("fold_stacked_branch", "integration", "bio_interp"),  # fold RIGHT via junction
        ("fold_double", "calling", "hard_filter"),  # fold turn across rows
    ],
)
def test_each_reversing_idiom_fires(stem: str, feeder: str, consumer: str) -> None:
    assert _verdict(stem, feeder, consumer) is True


def test_straight_continuation_preserves() -> None:
    """A forward RIGHT-exit -> LEFT-entry continuation is never reversed."""
    saw_continuation = False
    for path in CORPUS_FILES:
        for sig, classifier, _machinery in _seam_verdicts(str(path)):
            if sig[3] == "R->L":
                saw_continuation = True
                assert not classifier, f"{sig} should preserve"
    assert saw_continuation
