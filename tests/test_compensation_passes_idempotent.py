"""Each compensation pass is a geometric no-op when replayed after full
layout settling.

Named ``engine.py`` call sites exist purely to correct a side effect an
*earlier* stage introduced (a bbox push, a bbox grow, a consumer move) --
see ``COMPENSATION_PASSES`` in ``conftest.py`` for the stage/disturber table.
The property that matters for a compensation pass is not the back-to-back
``P(P(x)) == P(x))`` of ``tests/test_content_placement_idempotent.py``: because
a compensation pass exists to correct the disturber stage that ran before it,
the meaningful question is whether it remains a no-op once every later stage
has also run and the whole layout has settled. Finding movement
here is the start of an investigation, not proof of a bug: a later stage may
be violating the precondition the compensation pass assumed, but it may
instead have an independently documented, tested reason to diverge from it
on purpose (see the confirmed example in ``_KNOWN_END_OF_LAYOUT_GAPS``
below). Cross-check any other invariant test covering the same geometry and
render the fixture before concluding a fix belongs in the later stage.

Mechanism: monkeypatch each distinct helper function backing the stage
labels with a mock that wraps the real implementation (so the pipeline
computes its ordinary output) and records the call it was invoked with most
recently. Once the full corpus fixture's layout has settled, replay each
stage's helper(s) with that captured call directly on the settled graph,
diff against a snapshot taken just before, then restore. Restoring covers
ports as well as stations (see ``snapshot_graph_state``) so the diagnostic
replay leaves the graph's station/port pair in sync for anything running
after this test.
"""

from __future__ import annotations

from collections.abc import Callable
from unittest.mock import MagicMock

import pytest
from conftest import (
    COMPENSATION_PASSES,
    Diff,
    compute_corpus_layout,
    content_corpus,
    diff_station_coords,
    restore_graph_state,
    snapshot_graph_state,
    snapshot_stations,
)

import nf_metro.layout.engine as engine
from nf_metro.parser.model import MetroGraph

CORPUS = content_corpus()

_HELPER_NAMES = sorted({name for _, names in COMPENSATION_PASSES for name in names})

# Stages 6.7 through 6.9 in engine.py execute only when
# ``graph.center_ports or graph.diamond_style == "symmetric"``. A stage
# gated here is skipped for a fixture where that block never runs, so a
# helper's most-recently-recorded call -- from an earlier, unconditional
# call site that happens to share the same function -- is never mistaken
# for this stage's own execution.
_CONDITIONAL_STAGES: dict[str, Callable[[MetroGraph], bool]] = {
    "6.8": lambda graph: graph.center_ports or graph.diamond_style == "symmetric",
    "6.9": lambda graph: graph.center_ports or graph.diamond_style == "symmetric",
}

# Fixtures where a stage's compensation pass is not a no-op when replayed
# after full layout settling, keyed to the stage label(s) that reproduce it.
#
# The ``{"4.7"}`` entries are intentional.  Row flush is a transient property
# of the intermediate stages, not a final-state guarantee: Stage 6.15a's
# ``_fit_bboxes_to_content_top`` un-flushes a row-mate's bbox top to hug its
# own content whenever that section's top band is empty, so replaying Stage
# 4.7's ``_top_align_row_sections`` on the settled graph moves it back.
# ``test_section_bbox_top_hugs_content`` encodes the content-hug requirement
# these fixtures actually owe.
#
# ``topologies/bt_perp_left_entry_right_exit``'s "4.7" entry has a different
# cause: its box extends past its content to keep ``PERP_PORT_EDGE_INSET``
# beyond a perpendicular port (#1540), and with two such ports Stage 6.16's
# re-snap leaves one of them off the hug line
# ``refit_tops_after_entry_resnap`` settles the other against.
#
# ``topologies/tb_off_track_inputs``'s "6.6" entry is an open defect, not an
# intended divergence: replaying ``_reanchor_off_track_to_consumer`` swaps the
# X positions of two off-track sibling stations instead of reproducing them,
# so that pass is order-sensitive in X.
# Under the ``row_align: content`` default the packed-row header level-off
# (``_top_align_packed_row_bboxes``) is gated off, so a packed or
# content-hugging row-mate keeps its own content-fit top, and Stage 4.7's
# unconditional ``_top_align_row_sections`` replays non-idempotently on that
# settled top.  ``examples/riboseq_metro`` and the three packed topologies
# below carry a "4.7" gap for that reason.
_KNOWN_END_OF_LAYOUT_GAPS: dict[str, frozenset[str]] = {
    "examples/differentialabundance": frozenset({"4.7"}),
    "examples/differentialabundance_default": frozenset({"4.7"}),
    "examples/riboseq_metro": frozenset({"4.7"}),
    "examples/variantbenchmarking_auto": frozenset({"4.7"}),
    "tests/da_pipeline": frozenset({"4.7"}),
    "tests/trunk_align_matching_bundle": frozenset({"4.7"}),
    "topologies/bt_perp_left_entry_right_exit": frozenset({"4.7"}),
    "topologies/fan_branch_additional_outputs": frozenset({"4.7"}),
    "topologies/fanout_hub_two_line_trunk": frozenset({"4.7"}),
    "topologies/fanout_line_reused_nonadjacent_leg": frozenset({"4.7"}),
    "topologies/internal_source_equal_sibling_2fan": frozenset({"4.7"}),
    "topologies/near_edge_exit_corner": frozenset({"4.7"}),
    "topologies/off_track_convergence": frozenset({"4.7"}),
    "topologies/off_track_convergence_multiline": frozenset({"4.7"}),
    "topologies/off_track_input_above_consumer": frozenset({"4.7"}),
    "topologies/out_of_section_retag_fan": frozenset({"4.7"}),
    "topologies/packed_cell_cellmate_bypass_entry_y": frozenset({"4.7"}),
    "topologies/packed_cell_cellmate_bypass_no_handoff": frozenset({"4.7"}),
    "topologies/packed_multiline_serpentine_grid": frozenset({"4.7"}),
    "topologies/port_fed_three_branch_diamond": frozenset({"4.7"}),
    "topologies/ported_symmetric_fan_centreline_trunk": frozenset({"4.7"}),
    "topologies/rl_entry_right_exit_left": frozenset({"4.7"}),
    "topologies/rowmate_tb_side_entry_top_align_grow": frozenset({"4.7"}),
    "topologies/side_branch_ascent_label_strike": frozenset({"4.7"}),
    "topologies/symmetric_deadend_fanout": frozenset({"4.7"}),
    "topologies/symmetric_deadend_fanout_deep": frozenset({"4.7"}),
    "topologies/symmetric_deadend_fanout_exit": frozenset({"4.7"}),
    "topologies/symmetric_deadend_fanout_relay": frozenset({"4.7"}),
    "topologies/symmetric_join_exit_port_centre": frozenset({"4.7"}),
    "topologies/tb_off_track_inputs": frozenset({"6.6"}),
    "topologies/terminal_symmetric_fan": frozenset({"4.7"}),
    "topologies/top_descent_over_left_entry": frozenset({"4.7"}),
    "topologies/top_descent_over_left_entry_junction": frozenset({"4.7"}),
    "topologies/trunk_through_fan": frozenset({"4.7"}),
    "topologies/trunkless_entry_fan_reconverge_centre": frozenset({"4.7"}),
}


@pytest.mark.parametrize(
    "fid,path,is_nextflow", CORPUS, ids=[fid for fid, _, _ in CORPUS]
)
def test_compensation_pass_is_end_of_layout_noop(fid, path, is_nextflow, monkeypatch):
    """Every compensation pass is a no-op when replayed after full settling
    on ``fid``.

    All stage labels share one layout pass: their helpers are wrapped (not
    disturbed) while the real pipeline runs, then replayed in stage order on
    the settled graph, one at a time, each restored before the next runs so
    a failure in an earlier stage can't mask a later one.
    """
    original_fns = {name: getattr(engine, name) for name in _HELPER_NAMES}
    mocks = {name: MagicMock(wraps=original_fns[name]) for name in _HELPER_NAMES}
    for name, mock in mocks.items():
        monkeypatch.setattr(engine, name, mock)

    graph = compute_corpus_layout(path, is_nextflow)

    full_snap = snapshot_graph_state(graph)
    before = full_snap[0]

    diffs_by_stage: dict[str, list[Diff]] = {}
    for stage_label, helper_names in COMPENSATION_PASSES:
        gate = _CONDITIONAL_STAGES.get(stage_label)
        if gate is not None and not gate(graph):
            continue  # this stage's call site is never reached for this fixture
        if any(mocks[name].call_args is None for name in helper_names):
            continue  # this stage's call site never fired for this fixture

        for name in helper_names:
            call = mocks[name].call_args
            original_fns[name](graph, *call.args[1:], **call.kwargs)
        after = snapshot_stations(graph)
        diffs_by_stage[stage_label] = diff_station_coords(before, after)
        restore_graph_state(graph, full_snap)

    found_gaps = {label for label, diffs in diffs_by_stage.items() if diffs}
    expected_gaps = _KNOWN_END_OF_LAYOUT_GAPS.get(fid, frozenset())

    unexpected = found_gaps - expected_gaps
    assert not unexpected, (
        f"end-of-layout non-idempotence on {fid} not covered by "
        f"_KNOWN_END_OF_LAYOUT_GAPS: {sorted(unexpected)}. Stations per stage "
        f"(station: before -> after): "
        + "; ".join(
            f"{label}: {[(s, a, b) for s, a, b in diffs_by_stage[label][:8]]}"
            for label in sorted(unexpected)
        )
    )

    resolved = expected_gaps - found_gaps
    assert not resolved, (
        f"{fid} no longer reproduces the registered end-of-layout gap(s) "
        f"{sorted(resolved)}; remove the entry from _KNOWN_END_OF_LAYOUT_GAPS"
    )
