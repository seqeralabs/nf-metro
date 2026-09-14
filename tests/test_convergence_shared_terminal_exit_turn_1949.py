"""Regression locks for issue #1949.

The fixture is a grid-permuted variant of the #1806 convergence repro whose
merge is fed by two feeders where only one reaches the shared entry port. That
overshoot exposes two independent defects in the ``SHARED_TERMINAL_APPROACH``
convergence path:

1. An *uncovered* continuation was given the shared terminal axis's source point
   (the surviving carrier feeder's landing-runway start) instead of the start of
   its own emitted hop off the merge junction. The two coincide only when the
   carrier's terminal run begins at the merge itself; once a feeder overshoots
   the merge to a private port they differ, and the plan is internally
   inconsistent the moment it is built.

2. The carrier feeder's own exit turn is stranded when
   ``_settle_shared_source_openings`` fuses its opening descent onto the trunk
   source flank, so emission draws it running the wrong way off its section. The
   convergence must decline ownership (fall back to ``LEGACY``) rather than emit
   that route.

Defect 2's guard declines the fixture's convergence before the continuation is
built, so the fixture's clean render cannot by itself prove defect 1 is fixed.
The defect-1 lock therefore disables that guard and asserts on the planned
continuation directly.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import nf_metro.layout.routing.convergences as convergences
from nf_metro.layout.engine import compute_layout
from nf_metro.layout.routing.convergences import (
    COORD_TOLERANCE,
    ConvergenceTrunkReason,
    DemandAxis,
)
from nf_metro.parser.mermaid import parse_metro_mermaid

FIXTURE = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "topologies"
    / "convergence_shared_terminal_exit_turn.mmd"
)


def _graph():
    graph = parse_metro_mermaid(FIXTURE.read_text())
    graph.center_ports = True
    return graph


def test_carrier_exit_turn_forces_legacy_decline(monkeypatch):
    """Defect 2: the convergence declines rather than strand the carrier's exit turn.

    The resulting layout has no validator errors.
    """
    reasons: list[str] = []
    original = convergences._legacy_plan

    def record(scaffold, view, membership, reason):
        reasons.append(reason)
        return original(scaffold, view, membership, reason)

    monkeypatch.setattr(convergences, "_legacy_plan", record)

    graph = _graph()
    compute_layout(graph, validate=True)

    assert "convergence landing conflicts with an upstream exit turn" in reasons


def test_uncovered_continuation_starts_at_its_own_hop(monkeypatch):
    """Defect 1: an uncovered shared-terminal continuation starts at its own hop.

    Its start point is the start of its own emitted hop (the merge junction),
    not the shared axis source. Defect 2's guard is disabled so the convergence
    stays fused and the continuation is actually planned; otherwise the plan
    declines to ``LEGACY`` before this code path is reached.
    """
    monkeypatch.setattr(
        convergences, "_shared_terminal_landing_drops_exit_turn", lambda *a, **k: False
    )

    captured: list[tuple[tuple[float, float], tuple[float, float]]] = []
    original = convergences._build_planned_convergence

    def capture(graph, ctx, scaffold, view, membership, *args, **kwargs):
        plan = original(graph, ctx, scaffold, view, membership, *args, **kwargs)
        if (
            plan.owns_geometry
            and plan.primary_trunk_reason
            is ConvergenceTrunkReason.SHARED_TERMINAL_APPROACH
            and plan.trunk_axis is not None
            and plan.trunk_axis.axis is DemandAxis.X
        ):
            for continuation in plan.outgoing_continuations:
                if continuation.covered_by_member_id is not None:
                    continue
                merge = graph.stations[continuation.edge.source]
                captured.append((continuation.start_point, (merge.x, merge.y)))
        return plan

    monkeypatch.setattr(convergences, "_build_planned_convergence", capture)

    graph = _graph()
    # Emission re-exposes defect 2 (the guard is disabled), so the render aborts
    # after the plan is built; the plan capture above is what this test asserts.
    with pytest.raises(Exception):
        compute_layout(graph, validate=True)

    assert captured, "no uncovered shared-terminal continuation was planned"
    for start_point, merge_point in captured:
        assert (
            abs(start_point[0] - merge_point[0]) <= COORD_TOLERANCE
            and abs(start_point[1] - merge_point[1]) <= COORD_TOLERANCE
        ), f"continuation start {start_point} is off its merge junction {merge_point}"
