"""The lane plan's gap legs: each inter-column gap's landing legs and the way
they run, read by the exit-turn planner to hand a column standing on a leg
another leg runs against to the gap's joint allocation."""

from __future__ import annotations

import warnings
from pathlib import Path

import pytest
from test_co_row_diagonal_group import ENTRY_RUNWAY_SIBLING
from test_entry_frame_through_junction import STRANDED_BRANCH_MMD

import nf_metro.layout.routing.exit_turns as exit_turns_module
from nf_metro.api import render_string
from nf_metro.layout import engine as engine_module
from nf_metro.layout.lane_plan import GapLeg, gap_counter_runners
from nf_metro.parser.mermaid import parse_metro_mermaid
from nf_metro.parser.model import MetroGraph

MULTI_SOURCE = (
    Path(__file__).resolve().parent / "data" / "landing_deal" / "multi_source.mmd"
).read_text()


def _laid_out(text: str) -> MetroGraph:
    graph = parse_metro_mermaid(text)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        engine_module.compute_layout(graph)
    assert graph._lane_plan is not None
    return graph


def _legs(graph: MetroGraph) -> dict[int, set[tuple[str, str, tuple[str, ...], bool]]]:
    assert graph._lane_plan is not None
    return {
        gap: {(g.entry_id, g.source_id, g.lines, g.down) for g in legs}
        for gap, legs in graph._lane_plan.gap_legs.items()
    }


@pytest.mark.parametrize(
    ("text", "gap", "legs"),
    [
        (
            ENTRY_RUNWAY_SIBLING,
            0,
            {
                ("sec2__entry_left_2", "sec0__exit_right_1", ("l1",), True),
                ("sec1__entry_left_3", "__implicit____exit_right_0", ("l2",), False),
            },
        ),
        (
            STRANDED_BRANCH_MMD,
            1,
            {
                ("sec2__entry_left_2", "sec0__exit_right_0", ("l0", "l2"), False),
                ("sec3__entry_left_3", "sec1__exit_right_1", ("l1",), True),
            },
        ),
        (
            MULTI_SOURCE,
            2,
            {
                ("sec3__entry_right_5", "sec2__exit_right_1", ("l1", "l3"), True),
                ("sec3__entry_right_5", "sec1__exit_right_2", ("l2",), True),
                ("sec4__entry_left_6", "sec3__exit_right_3", ("l2",), False),
            },
        ),
    ],
    ids=["entry_runway_sibling", "stranded_branch", "multi_source"],
)
def test_a_gap_records_the_legs_landing_in_it_and_their_way(
    text: str, gap: int, legs: set[tuple[str, str, tuple[str, ...], bool]]
) -> None:
    assert _legs(_laid_out(text))[gap] == legs


RL_RETURN = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "curve_invariant_repros"
    / "rl_return_row_convergence.mmd"
).read_text()


def test_a_connector_from_beyond_the_gaps_other_column_lands_no_leg() -> None:
    recorded = {
        (entry, source)
        for legs in _legs(_laid_out(RL_RETURN)).values()
        for entry, source, _lines, _down in legs
    }
    assert ("cnv_calling__entry_right_10", "small_variants__exit_right_2") not in (
        recorded
    )
    assert ("annotation__entry_right_14", "structural_variants__exit_right_5") not in (
        recorded
    )


def test_a_level_or_off_column_connector_lands_no_leg() -> None:
    recorded = {
        (entry, source)
        for legs in _legs(_laid_out(STRANDED_BRANCH_MMD)).values()
        for entry, source, _lines, _down in legs
    }
    # sec1's level feeder and sec1's level branch into sec2 run straight in.
    assert ("sec1__entry_left_4", "sec0__exit_right_0") not in recorded
    assert ("sec2__entry_left_2", "sec1__exit_right_1") not in recorded


def test_counter_runners_share_a_row_and_run_the_other_way() -> None:
    graph = _laid_out(MULTI_SOURCE)
    found = gap_counter_runners(graph, "sec4__entry_left_6", "l2")
    assert found is not None
    gap, against = found
    assert gap == 2
    assert {(g.entry_id, g.lines) for g in against} == {
        ("sec3__entry_right_5", ("l1", "l3")),
        ("sec3__entry_right_5", ("l2",)),
    }
    assert all(isinstance(g, GapLeg) and g.down for g in against)
    assert gap_counter_runners(graph, "sec1__entry_left_4", "l0") is None


def test_a_column_beside_a_counter_runner_waits_for_the_joint_allocation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    built: list[tuple[str, str | None, tuple[float, ...]]] = []
    real = exit_turns_module.build_exit_turn_execution

    def recording(*args: object, **kwargs: object) -> object:
        execution = real(*args, **kwargs)  # type: ignore[arg-type]
        built.extend(
            (p.source_id, p.legacy_reason, tuple(a.coordinate for a in p.axes))
            for p in execution.plans
            if p.source_id == "__junction_4"
        )
        return execution

    monkeypatch.setattr(exit_turns_module, "build_exit_turn_execution", recording)
    svg = render_string(ENTRY_RUNWAY_SIBLING)
    assert svg
    first, *later = built
    assert first[1] == exit_turns_module.GAP_ALLOCATION_PENDING
    assert later and later[-1][1] is None
    assert later[-1][2]
