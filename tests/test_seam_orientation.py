"""Corpus oracle for the seam-orientation classifier (issue #1040).

:func:`seam_orientation` is a pure, seam-local primitive: given a feeding exit
port and the entry port it feeds, it returns whether the bundle order arrives
*preserved* or *reversed*, derived only from port sides and grid topology. These
tests pin it against the legacy reversal machinery across every inter-section
seam in the corpus.

Two guarantees:

* **Soundness** -- every classifier transposition is either reproduced by the
  machinery or belongs to the pinned set of direct half-turn seams that the
  seam-local model owns.
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

# The opposite direction: seams the seam-local classifier reverses and the
# machinery does not, because the machinery carries no section-absolute flag for
# a direct half-turn.  Same key shape as the residual set above.
EXPECTED_CLASSIFIER_ONLY_REVERSALS = frozenset(
    {
        ("stacked_left_exit_drop", "sec1", "sec2", "L->L"),
        ("stacked_multiline_left_exit_drop", "source", "target", "L->L"),
        ("stacked_split_left_entry_drop", "source", "target", "L->L"),
        (
            "plan_owned_distinct_lane_separation",
            "shared_source",
            "target_secondary",
            "L->R",
        ),
        # A LEFT-exit fan's side branch: line h leaves b's LEFT exit and arrives
        # at d's RIGHT entry one row down, so the delivered bundle re-nests.
        ("left_exit_fan_perp_entry_landing", "b", "d", "L->R"),
        # The RIGHT-facing mirror of that stacked LEFT half-turn: an LR row's
        # RIGHT exit descending into an RL row's RIGHT entry at or right of the
        # feeder's column.  Leaving rightward and arriving rightward is a net
        # half-turn, so the delivered bundle has to re-nest; every seam below is
        # that shape, with the feeder row above the consumer row.
        #
        # aux (col 0, row 1) descends two columns right into the RL repeats row.
        ("convergence_stacked_sink", "aux", "repeats", "R->R"),
        # feeder_a (col 0, row 0) drops one column right into the RL source row.
        ("leftward_up_exit_turn_order", "feeder_a", "source", "R->R"),
        # source (col 0, row 0) clears an empty row to reach target at col 2.
        ("right_entry_gap_above_empty_row", "source", "target", "R->R"),
        # source wraps past target's outward edge and doubles back into it.
        ("right_entry_wrap_no_fan", "source", "target", "R->R"),
        # The multi-line member of that wrap, where the re-nesting is visible.
        ("right_entry_wrap_bundle", "source", "target", "R->R"),
        # Two single-line feeders sharing row 0, one per column, converge on one
        # RIGHT entry in the row below; each takes the same half-turn into it.
        ("stacked_right_ports_coincident", "above", "below", "R->R"),
        ("stacked_right_ports_coincident", "feeder", "below", "R->R"),
        # The RIGHT-facing mirror of stacked_split_left_entry_drop, whose L->L
        # seam sits in this same set: the half-turn that feeds an internal split.
        ("stacked_split_right_entry_drop", "source", "target", "R->R"),
        # The packed-cell serpentine fold: a two-line bundle folding from an LR
        # row into the RL return row stacked beneath it.
        (
            "serpentine_rl_right_entry_bundle",
            "quantification",
            "variant_calling",
            "R->R",
        ),
    }
)
EXPECTED_RESIDUALS = frozenset(
    {
        NEAR_VERTICAL_RESIDUAL,
        # positive_fan TB (right-entry) feeding a vertical TB column below it:
        # the machinery marks the lower section as positive_fan so it draws on
        # the same +x side as the drop, but the classifier sees a vertical→vertical
        # continuation and correctly says PRESERVE (no bundle-order flip).
        ("tb_right_entry_stack", "upper", "lower", "B->T"),
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
        # True-serpentine fold: a horizontal-flow row folds down through a BOTTOM
        # exit into a horizontal-flow return row, whose descend->turn corner
        # reverses the bundle order.  The machinery marks the receiver (and its
        # row successors) reversed, but the classifier preserves the seam.
        ("serpentine_rl_bundle", "variant_calling", "normalization", "B->T"),
        ("serpentine_rl_bundle", "normalization", "consensus", "L->R"),
        ("serpentine_rl_bundle", "consensus", "realignment", "L->R"),
        ("branch_fold_forward", "post", "report", "B->T"),
        # Auto-folded serpentine return row: the fold reverses consensus, which
        # propagates along the row to realign and reporting (reached through the
        # exit peel-off junction).  The realign->reporting seam is a plain L->R
        # continuation the classifier preserves while the machinery reverses it.
        ("foldback_exit_peeloff", "realign", "reporting", "L->R"),
        # Manual-grid LR bottom-exit dropping into a direction: RL top entry
        # stacked below it: the fold reverses consensus (the B->T drop receiver)
        # and propagates along the return row to reporting (reached through the
        # exit peel-off junction).  Both seams are continuations the classifier
        # preserves while the machinery reverses them.
        ("lr_bottom_exit_rl_top_entry_jog", "normalization", "consensus", "B->T"),
        ("lr_bottom_exit_rl_top_entry_jog", "realign", "reporting", "L->R"),
        # Manual-grid serpentine whose return row flows direction: RL: the fold
        # reverses consensus (the B->T drop receiver) and propagates along the
        # return row to reporting.  Both seams are continuations the classifier
        # preserves while the machinery reverses them.
        ("manual_rl_row_nonconsumer_bypass", "normalization", "consensus", "B->T"),
        ("manual_rl_row_nonconsumer_bypass", "realign", "reporting", "L->R"),
        # Same return-row reversal as manual_rl_row_nonconsumer_bypass, but
        # realign and reporting are packed into a single grid cell rather than
        # each owning its own.
        ("packed_cell_cellmate_bypass", "normalization", "consensus", "B->T"),
        ("packed_cell_cellmate_bypass", "realign", "reporting", "L->R"),
        # Same packed-cell return-row reversal with the bypass source sitting in
        # the column adjacent to the packed cell (no gap column).
        ("packed_cell_cellmate_bypass_adjacent", "normalization", "consensus", "B->T"),
        ("packed_cell_cellmate_bypass_adjacent", "realign", "reporting", "L->R"),
        # Folded corridor whose return row carries each line on its own lane: the
        # fold reverses the B->T drop receiver (normalization) and propagates
        # along the return row to realignment.  Both are continuations the
        # classifier preserves while the machinery reverses them.
        ("folded_corridor_distinct_lanes", "variant_calling", "normalization", "B->T"),
        ("folded_corridor_distinct_lanes", "consensus", "realignment", "L->R"),
        # A column of LR sections chained BOTTOM exit -> TOP entry: each drop is a
        # B->T continuation the classifier preserves, while the machinery marks
        # every receiver reversed off the section-absolute BOTTOM-exit flag.
        ("lr_perp_top_entry_bottom_exit", "intake", "mid", "B->T"),
        ("lr_perp_top_entry_bottom_exit", "mid", "report", "B->T"),
        # The multi-line members of that same family, plus the RIGHT-exit
        # continuation the marked receiver propagates along its own row.
        ("lr_top_entry_bundle_east_turn", "intake", "align", "B->T"),
        ("lr_top_entry_bundle_east_turn", "align", "report", "R->L"),
        ("rl_bottom_exit_lr_top_entry_bundle", "intake", "align", "B->T"),
        # A TB LEFT exit marks its same-row horizontal consumer reversed.  The
        # seam itself is a straight L->R continuation, so the classifier
        # preserves the delivered bundle order.
        (
            "multi_frame_exit_lane_settlement",
            "side_work",
            "side_report",
            "L->R",
        ),
        # A two-line bundle drops near-vertically from an LR row's RIGHT exit into
        # the RIGHT entry of the RL row directly below: the machinery marks the
        # receiver reversed off the near-vertical-junction-right-entry rule, while
        # the classifier reads the descend->turn seam as a preserved continuation.
        ("reversed_section_junction_reseat", "align", "quant", "R->R"),
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
def test_classifier_only_reversals_are_pinned_half_turns(path: Path) -> None:
    """Classifier-only reversals are exactly the direct half-turn seams."""
    classifier_only = {
        sig
        for sig, classifier, machinery in _seam_verdicts(str(path))
        if classifier and not machinery
    }
    expected = {
        sig for sig in EXPECTED_CLASSIFIER_ONLY_REVERSALS if sig[0] == path.stem
    }
    assert classifier_only == expected, (
        f"{path.stem}: classifier-only seam reversals differ: "
        f"actual={classifier_only}, expected={expected}"
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
        ("stacked_multiline_left_exit_drop", "source", "target"),  # stacked LEFT
        # stacked RIGHT
        ("serpentine_rl_right_entry_bundle", "quantification", "variant_calling"),
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
