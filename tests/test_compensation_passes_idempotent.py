"""Each compensation pass is a geometric no-op when replayed after full
layout settling.

Six ``engine.py`` call sites exist purely to correct a side effect an
*earlier* stage introduced (a bbox push, a bbox grow, a consumer move) --
see ``COMPENSATION_PASSES`` in ``conftest.py`` for the stage/disturber table.
The property that matters for a compensation pass is not
``test_content_placement_idempotent``'s back-to-back ``P(P(x)) == P(x))``:
because a compensation pass exists to correct the disturber stage that ran
before it, the meaningful question is whether it remains a no-op once every
later stage has also run and the whole layout has settled. If invoking it
again at that point moves something, a later stage violates the
precondition the compensation pass assumed, and that later stage is where
the real fix belongs.

Mechanism: monkeypatch each of the five distinct helper functions backing
the six stage labels with a probe that applies the real pass (so the
pipeline computes its ordinary output) and records the ``(args, kwargs)``
it was invoked with most recently. Once the full corpus fixture's layout has
settled, replay each stage's helper(s) with those captured arguments
directly on the settled graph, diff against a snapshot taken just before,
then restore. Restoring covers ports as well as stations (see
``snapshot_graph_state``) so the diagnostic replay leaves the graph's
station/port pair in sync for anything running after this test.
"""

from __future__ import annotations

import pytest
from conftest import (
    COMPENSATION_PASSES,
    compute_corpus_layout,
    content_corpus,
    restore_graph_state,
    snapshot_graph_state,
)

import nf_metro.layout.engine as engine
from nf_metro.parser.model import MetroGraph

CORPUS = content_corpus()

TOL = 1e-6

_Point = tuple[float, float]
_Diff = tuple[str, _Point | None, _Point | None]

_HELPER_NAMES = sorted({name for _, names in COMPENSATION_PASSES for name in names})

_Call = tuple[tuple, dict]

# Fixtures where a stage's compensation pass is not currently a no-op when
# replayed after full layout settling, keyed to the stage label(s) that
# reproduce it. In every ``{"3.5", "4.7"}`` case the mechanism is the same:
# a section grid-row-mate's bbox top grows upward later in the layout (fan
# content redistributed above the trunk, or an off-track lift) without a
# corresponding shift for sibling sections in its row, breaking the row-top
# flush alignment ``_top_align_row_sections`` established by the time the
# whole layout has settled. ``topologies/tb_off_track_inputs``'s "6.8" entry is
# a distinct mechanism: replaying ``_reanchor_off_track_to_consumer`` swaps
# the X positions of two off-track sibling stations instead of reproducing
# them, so the pass is order-sensitive rather than idempotent.
#
# Entries are removed only when the underlying stage genuinely becomes an
# end-of-layout no-op; the assertions below fail loudly both on any new,
# unregistered gap and on any registered gap that stops reproducing, so this
# dict can't silently drift out of sync with engine behaviour.
_KNOWN_END_OF_LAYOUT_GAPS: dict[str, frozenset[str]] = {
    "examples/differentialabundance": frozenset({"3.5", "4.7"}),
    "examples/differentialabundance_default": frozenset({"3.5", "4.7"}),
    "tests/da_pipeline": frozenset({"3.5", "4.7"}),
    "tests/trunk_align_matching_bundle": frozenset({"3.5", "4.7"}),
    "topologies/exit_fan_label_strike": frozenset({"3.5", "4.7"}),
    "topologies/fanout_hub_two_line_trunk": frozenset({"3.5", "4.7"}),
    "topologies/internal_source_equal_sibling_2fan": frozenset({"3.5", "4.7"}),
    "topologies/off_track_convergence": frozenset({"3.5", "4.7"}),
    "topologies/off_track_convergence_multiline": frozenset({"3.5", "4.7"}),
    "topologies/off_track_input_above_consumer": frozenset({"3.5", "4.7"}),
    "topologies/packed_multiline_serpentine_grid": frozenset({"3.5", "4.7"}),
    "topologies/rl_entry_right_exit_left": frozenset({"3.5", "4.7"}),
    "topologies/rowmate_tb_side_entry_top_align_grow": frozenset({"3.5", "4.7"}),
    "topologies/shared_cell_fork_trunk_align": frozenset({"3.5", "4.7"}),
    "topologies/symmetric_deadend_fanout": frozenset({"3.5", "4.7"}),
    "topologies/symmetric_deadend_fanout_deep": frozenset({"3.5", "4.7"}),
    "topologies/symmetric_deadend_fanout_exit": frozenset({"3.5", "4.7"}),
    "topologies/symmetric_deadend_fanout_relay": frozenset({"3.5", "4.7"}),
    "topologies/tb_off_track_inputs": frozenset({"6.8"}),
    "topologies/terminal_symmetric_fan": frozenset({"3.5", "4.7"}),
    "topologies/trunk_through_fan": frozenset({"3.5", "4.7"}),
}


def _make_capture_probe(original, last_call: dict[str, _Call], name: str):
    """Wrap ``original`` to apply it for real and record the most recent
    ``(args, kwargs)`` it was invoked with under ``name``."""

    def probe(graph: MetroGraph, *args, **kwargs):
        original(graph, *args, **kwargs)
        last_call[name] = (args, kwargs)

    return probe


def _diff_stations(before: dict[str, _Point], after: dict[str, _Point]) -> list[_Diff]:
    diffs: list[_Diff] = []
    for sid, (x1, y1) in before.items():
        if sid not in after:
            diffs.append((sid, (x1, y1), None))
        elif abs(after[sid][0] - x1) > TOL or abs(after[sid][1] - y1) > TOL:
            diffs.append((sid, (x1, y1), after[sid]))
    for sid in after.keys() - before.keys():
        diffs.append((sid, None, after[sid]))
    return diffs


@pytest.mark.parametrize(
    "fid,path,is_nextflow", CORPUS, ids=[fid for fid, _, _ in CORPUS]
)
def test_compensation_pass_is_end_of_layout_noop(fid, path, is_nextflow, monkeypatch):
    """Every compensation pass is a no-op when replayed after full settling
    on ``fid``.

    All six stage labels share one layout pass: their helpers are captured
    (not disturbed) while the real pipeline runs, then replayed in stage
    order on the settled graph, one at a time, each restored before the
    next runs so a failure in an earlier stage can't mask a later one.
    """
    original_fns = {name: getattr(engine, name) for name in _HELPER_NAMES}
    last_call: dict[str, _Call] = {}
    for name in _HELPER_NAMES:
        monkeypatch.setattr(
            engine, name, _make_capture_probe(original_fns[name], last_call, name)
        )

    graph = compute_corpus_layout(path, is_nextflow)

    diffs_by_stage: dict[str, list[_Diff]] = {}
    for stage_label, helper_names in COMPENSATION_PASSES:
        missing = [name for name in helper_names if name not in last_call]
        if missing:
            continue  # this stage's call site never fired for this fixture

        snap = snapshot_graph_state(graph)
        before = snap[0]
        for name in helper_names:
            args, kwargs = last_call[name]
            original_fns[name](graph, *args, **kwargs)
        after = snapshot_graph_state(graph)[0]
        diffs_by_stage[stage_label] = _diff_stations(before, after)
        restore_graph_state(graph, snap)

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
