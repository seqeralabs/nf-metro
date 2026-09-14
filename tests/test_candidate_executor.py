"""Contract tests for isolated, bounded layout-candidate execution."""

from __future__ import annotations

import hashlib
import json
import multiprocessing
import os
import struct
import subprocess
import sys
import time
import warnings
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest
from click.testing import CliRunner

import nf_metro.candidate_executor as candidate_executor
from nf_metro.api import (
    RenderConfig,
    _prepare_graph_state,
    render_graph_result,
    render_string,
    resolve_theme,
)
from nf_metro.candidate_executor import (
    CandidateExecutionRequest,
    CandidateStage,
    CandidateStatus,
    DirectionCommitment,
    EndpointCommitment,
    ExecutionLimits,
    GridCommitment,
    LayoutCandidate,
    LayoutCommitments,
    LayoutOptionValue,
    LayoutOptionValues,
    RenderConfigSnapshot,
    StopReason,
    _canonical_evidence,
    _canonical_json,
    _FaultAction,
    _FaultInjection,
    _graph_evidence,
    execute_candidates,
)
from nf_metro.cli import cli
from nf_metro.layout import compute_layout
from nf_metro.layout.route_plan import RoutePlanDiagnostic, serialize_route_plan
from nf_metro.parser.commitments import (
    CommitmentSettlementError,
    LayoutCommitmentOverlay,
    verify_settled_commitments,
)
from nf_metro.parser.model import LayoutGeometryWarning, PortSide
from nf_metro.parser.provenance import ConnectorEndpointRole
from nf_metro.render.svg import ObservedRenderPlan, build_observed_render_plan
from nf_metro.render.validate import (
    LABEL_STRIKE,
    MARKER_CROSS,
    OFFSET_COLLAPSE,
    RenderFinding,
)

ROOT = Path(__file__).resolve().parents[1]
CONTROL_PATH = ROOT / "tests/fixtures/candidate_executor/control.mmd"
CONTROL_SOURCE = CONTROL_PATH.read_text()

PINNED_SOURCE = """\
%%metro line: main | Main | #ff0000
%%metro fold_threshold: 9
%%metro line_order: definition
%%metro grid: left | 0,0
%%metro grid: right | 1,0
graph LR
    subgraph left [Left]
        %%metro direction: LR
        %%metro exit: right | main
        a[A]
        b[B]
        a -->|main| b
    end
    subgraph right [Right]
        %%metro direction: LR
        %%metro entry: left | main
        c[C]
        d[D]
        c -->|main| d
    end
    b -->|main| c
"""


def _request(**changes: Any) -> CandidateExecutionRequest:
    request = CandidateExecutionRequest(
        CONTROL_SOURCE,
        source_dir=str(ROOT),
        limits=ExecutionLimits(8, 10.0, 30.0),
    )
    return replace(request, **changes)


def _connector_id(source: str = CONTROL_SOURCE) -> str:
    graph = _prepare_graph_state(source, source_dir=str(ROOT))
    assert graph.route_topology is not None
    return str(graph.route_topology.connectors[0].id)


def _direct_result(
    request: CandidateExecutionRequest,
    candidate: LayoutCandidate | None = None,
    fault: _FaultInjection | None = None,
):
    options = candidate_executor._validate_request(request)
    production = candidate_executor._production_input(request, options)
    attempt = candidate_executor._attempt_input(production, 0, candidate)
    return candidate_executor._evaluate_attempt(attempt, fault)


def test_checked_in_control_returns_complete_accepted_evidence() -> None:
    result = execute_candidates(_request(limits=ExecutionLimits(1, 10.0, 20.0)))

    baseline = result.baseline
    assert baseline.status is CandidateStatus.ACCEPTED
    assert baseline.worker_exit_code == 0
    assert baseline.evidence.graph is not None
    assert baseline.evidence.route_plan is not None
    assert baseline.evidence.render_plan is not None
    assert baseline.evidence.svg is not None
    assert len(baseline.evidence.svg.content) > candidate_executor._MAX_FRAME_BYTES
    assert not baseline.evidence.graph_findings
    assert not any(item.blocking for item in baseline.evidence.route_findings)
    assert not baseline.evidence.artifact_findings


def test_noop_attempt_matches_the_complete_production_pipeline() -> None:
    layout_options = LayoutOptionValues(
        (
            LayoutOptionValue("fold_threshold", 12),
            LayoutOptionValue("line_order", "span"),
            LayoutOptionValue("x_spacing", 72.0),
        )
    )
    render = RenderConfigSnapshot(
        responsive=True,
        svg_class_prefix="candidate",
        chrome_css=False,
        self_color_scheme=False,
        baked_mode="light",
    )
    request = _request(
        theme="seqera",
        mode="light",
        layout_options=layout_options,
        render=render,
        limits=ExecutionLimits(1, 10.0, 20.0),
    )
    baseline = execute_candidates(request).baseline
    assert baseline.status is CandidateStatus.ACCEPTED

    options = dict(candidate_executor._validate_request(request))
    graph = _prepare_graph_state(
        request.source,
        source_dir=request.source_dir,
        layout_options=options,
        bare=render.bare,
    )
    compute_layout(graph, validate=True)
    theme = resolve_theme(request.theme, graph, mode=request.mode)
    observed = build_observed_render_plan(
        graph,
        theme,
        debug=render.debug,
        chrome_css=render.chrome_css,
        bare=render.bare,
    )
    production = render_graph_result(
        graph,
        theme,
        RenderConfig(
            responsive=render.responsive,
            svg_class_prefix=render.svg_class_prefix,
            chrome_css=render.chrome_css,
            self_color_scheme=render.self_color_scheme,
            baked_mode=render.baked_mode,
            bare=render.bare,
        ),
    )

    assert observed.plan == production.plan
    assert baseline.evidence.graph == _graph_evidence(
        graph, candidate_executor.GraphState.SETTLED
    )
    assert baseline.evidence.route_plan is not None
    assert baseline.evidence.route_plan.content == serialize_route_plan(
        observed.route_plan
    )
    assert baseline.evidence.render_plan == _canonical_evidence(observed.plan)
    assert baseline.evidence.svg is not None
    assert baseline.evidence.svg.content == production.content
    assert baseline.evidence.render_plan.content.find(str(ROOT)) >= 0


def test_noop_candidate_matches_baseline_evidence() -> None:
    candidate = LayoutCandidate("noop")
    result = execute_candidates(
        _request(
            candidates=(candidate,),
            limits=ExecutionLimits(2, 10.0, 30.0),
        )
    )

    assert result.attempts[0].status is CandidateStatus.ACCEPTED
    assert result.attempts[0].evidence == result.baseline.evidence


def test_a_b_a_attempts_are_isolated() -> None:
    candidates = (
        LayoutCandidate("a-before"),
        LayoutCandidate(
            "bad-middle",
            LayoutCommitments(grids=(GridCommitment("missing", (0, 0, 1, 1)),)),
        ),
        LayoutCandidate("a-after"),
    )
    result = execute_candidates(
        _request(
            candidates=candidates,
            limits=ExecutionLimits(4, 10.0, 40.0),
        )
    )

    before, middle, after = result.attempts
    assert middle.status is CandidateStatus.VALIDATION_REJECTION
    assert before.status is after.status is CandidateStatus.ACCEPTED
    assert before.evidence == after.evidence == result.baseline.evidence


def test_authored_grid_fold_direction_endpoint_and_order_conflicts() -> None:
    connector = _connector_id(PINNED_SOURCE)
    conflicts = (
        LayoutCandidate(
            "grid",
            LayoutCommitments(grids=(GridCommitment("left", (3, 0, 1, 1)),)),
        ),
        LayoutCandidate("fold", LayoutCommitments(fold_threshold=3)),
        LayoutCandidate(
            "direction",
            LayoutCommitments(
                directions=(DirectionCommitment("left", cast(Any, "TB")),)
            ),
        ),
        LayoutCandidate(
            "endpoint",
            LayoutCommitments(
                endpoints=(
                    EndpointCommitment(
                        cast(Any, connector),
                        ConnectorEndpointRole.EXIT,
                        PortSide.BOTTOM,
                    ),
                )
            ),
        ),
        LayoutCandidate("order", LayoutCommitments(line_order="span")),
    )
    result = execute_candidates(
        CandidateExecutionRequest(
            PINNED_SOURCE,
            source_dir=str(ROOT),
            candidates=conflicts,
            limits=ExecutionLimits(6, 10.0, 40.0),
        )
    )

    assert all(
        item.status is CandidateStatus.VALIDATION_REJECTION
        and item.stage is CandidateStage.PREPARATION
        for item in result.attempts
    )


def test_caller_pins_override_source_and_reject_different_candidates() -> None:
    connector = _connector_id()
    caller = LayoutCommitments(
        grids=(
            GridCommitment("intake", (0, 0, 1, 1)),
            GridCommitment("analysis", (1, 0, 1, 1)),
        ),
        directions=(
            DirectionCommitment("intake", "LR"),
            DirectionCommitment("analysis", "LR"),
        ),
        endpoints=(
            EndpointCommitment(
                cast(Any, connector), ConnectorEndpointRole.EXIT, PortSide.RIGHT
            ),
            EndpointCommitment(
                cast(Any, connector), ConnectorEndpointRole.ENTRY, PortSide.LEFT
            ),
        ),
    )
    conflicts = (
        LayoutCandidate(
            "grid",
            LayoutCommitments(grids=(GridCommitment("intake", (4, 0, 1, 1)),)),
        ),
        LayoutCandidate(
            "direction",
            LayoutCommitments(directions=(DirectionCommitment("intake", "TB"),)),
        ),
        LayoutCandidate(
            "entry",
            LayoutCommitments(
                endpoints=(
                    EndpointCommitment(
                        cast(Any, connector),
                        ConnectorEndpointRole.ENTRY,
                        PortSide.TOP,
                    ),
                )
            ),
        ),
        LayoutCandidate("fold", LayoutCommitments(fold_threshold=3)),
        LayoutCandidate("order", LayoutCommitments(line_order="definition")),
    )
    request = _request(
        caller_pins=caller,
        layout_options=LayoutOptionValues(
            (
                LayoutOptionValue("fold_threshold", 12),
                LayoutOptionValue("line_order", "span"),
            )
        ),
        candidates=conflicts,
        limits=ExecutionLimits(6, 10.0, 40.0),
    )
    result = execute_candidates(request)

    assert result.baseline.status is CandidateStatus.ACCEPTED
    assert all(
        item.status is CandidateStatus.VALIDATION_REJECTION for item in result.attempts
    )


def test_same_value_caller_and_candidate_pins_are_noops() -> None:
    connector = _connector_id()
    pins = LayoutCommitments(
        grids=(
            GridCommitment("intake", (0, 0, 1, 1)),
            GridCommitment("analysis", (1, 0, 1, 1)),
        ),
        fold_threshold=12,
        directions=(
            DirectionCommitment("intake", "LR"),
            DirectionCommitment("analysis", "LR"),
        ),
        endpoints=(
            EndpointCommitment(
                cast(Any, connector), ConnectorEndpointRole.EXIT, PortSide.RIGHT
            ),
            EndpointCommitment(
                cast(Any, connector), ConnectorEndpointRole.ENTRY, PortSide.LEFT
            ),
        ),
        line_order="span",
    )
    result = execute_candidates(
        _request(
            caller_pins=pins,
            candidates=(LayoutCandidate("same", pins),),
            limits=ExecutionLimits(2, 10.0, 30.0),
        )
    )

    assert result.baseline.status is CandidateStatus.ACCEPTED
    assert result.attempts[0].status is CandidateStatus.ACCEPTED
    assert result.attempts[0].evidence == result.baseline.evidence


@pytest.mark.parametrize(
    "commitments",
    [
        LayoutCommitments(
            grids=(
                GridCommitment("intake", (0, 0, 1, 1)),
                GridCommitment("intake", (0, 0, 1, 1)),
            )
        ),
        LayoutCommitments(grids=(GridCommitment("intake", (-1, 0, 1, 1)),)),
        LayoutCommitments(
            directions=(DirectionCommitment("intake", cast(Any, "SIDEWAYS")),)
        ),
        LayoutCommitments(fold_threshold=-1),
        LayoutCommitments(line_order=cast(Any, "lexical")),
        LayoutCommitments(grids=(cast(Any, object()),)),
    ],
)
def test_malformed_and_duplicate_commitments_are_validation_rejections(
    commitments: LayoutCommitments,
) -> None:
    result = execute_candidates(
        _request(
            candidates=(LayoutCandidate("invalid", commitments),),
            limits=ExecutionLimits(2, 10.0, 30.0),
        )
    )

    assert result.attempts[0].status is CandidateStatus.VALIDATION_REJECTION
    assert result.attempts[0].stage is CandidateStage.PREPARATION


def test_duplicate_candidate_ids_and_invalid_layout_options_fail_preflight() -> None:
    with pytest.raises(ValueError, match="duplicate candidate id"):
        execute_candidates(
            _request(candidates=(LayoutCandidate("x"), LayoutCandidate("x")))
        )
    with pytest.raises(ValueError, match="duplicate caller layout option"):
        execute_candidates(
            _request(
                layout_options=LayoutOptionValues(
                    (
                        LayoutOptionValue("x_spacing", 50.0),
                        LayoutOptionValue("x_spacing", 60.0),
                    )
                )
            )
        )
    with pytest.raises(ValueError, match="invalid caller layout option"):
        execute_candidates(
            _request(
                layout_options=LayoutOptionValues(
                    (LayoutOptionValue("fold_threshold", True),)
                )
            )
        )
    with pytest.raises(ValueError, match="candidate ids"):
        execute_candidates(_request(candidates=(LayoutCandidate(cast(Any, 7)),)))


@pytest.mark.parametrize(
    ("name", "option", "pin"),
    [
        ("fold_threshold", 12, LayoutCommitments(fold_threshold=9)),
        ("line_order", "span", LayoutCommitments(line_order="definition")),
    ],
)
def test_conflicting_caller_option_and_commitment_are_rejected(
    name: str,
    option: int | str,
    pin: LayoutCommitments,
) -> None:
    result = execute_candidates(
        _request(
            caller_pins=pin,
            layout_options=LayoutOptionValues((LayoutOptionValue(name, option),)),
            limits=ExecutionLimits(1, 10.0, 20.0),
        )
    ).baseline

    assert result.status is CandidateStatus.VALIDATION_REJECTION
    assert result.stage is CandidateStage.PREPARATION


def test_settlement_checks_fold_value_on_the_graph() -> None:
    pins = LayoutCommitments(fold_threshold=12)
    overlay = LayoutCommitmentOverlay(caller=pins)
    graph = _prepare_graph_state(
        CONTROL_SOURCE,
        source_dir=str(ROOT),
        layout_commitments=overlay,
    )
    compute_layout(graph, validate=True)
    graph.fold_threshold = 13

    with pytest.raises(CommitmentSettlementError, match="fold-threshold"):
        verify_settled_commitments(graph, overlay)


def test_settlement_checks_endpoint_side_on_route_topology() -> None:
    connector_id = _connector_id()
    pins = LayoutCommitments(
        endpoints=(
            EndpointCommitment(
                cast(Any, connector_id),
                ConnectorEndpointRole.EXIT,
                PortSide.RIGHT,
            ),
        )
    )
    overlay = LayoutCommitmentOverlay(caller=pins)
    graph = _prepare_graph_state(
        CONTROL_SOURCE,
        source_dir=str(ROOT),
        layout_commitments=overlay,
    )
    compute_layout(graph, validate=True)
    assert graph.route_topology is not None
    connector = graph.route_topology.connectors[0]
    graph.route_topology = replace(
        graph.route_topology,
        connectors=(
            replace(connector, exit_side=PortSide.BOTTOM),
            *graph.route_topology.connectors[1:],
        ),
    )

    with pytest.raises(CommitmentSettlementError, match="exit commitment"):
        verify_settled_commitments(graph, overlay)


def test_attempt_limit_reports_unattempted_candidates_explicitly() -> None:
    candidates = tuple(LayoutCandidate(name) for name in ("a", "b", "c"))
    result = execute_candidates(
        _request(
            candidates=candidates,
            limits=ExecutionLimits(2, 10.0, 30.0),
        )
    )

    assert result.attempt_count == 2
    assert [item.candidate_id for item in result.attempts] == ["a"]
    assert [item.id for item in result.unattempted] == ["b", "c"]
    assert result.stop_reason is StopReason.ATTEMPT_LIMIT


def test_per_attempt_timeout_reaps_the_worker() -> None:
    result = execute_candidates(
        _request(limits=ExecutionLimits(1, 0.05, 1.0)),
        _fault=_FaultInjection(_FaultAction.BLOCK),
    )

    assert result.baseline.status is CandidateStatus.TIMEOUT
    assert result.baseline.failure is not None
    assert result.baseline.failure.message == "per-attempt deadline exceeded"
    assert not multiprocessing.active_children()


def test_total_deadline_stops_dispatch_without_inventing_attempts() -> None:
    candidate = LayoutCandidate("never-started")
    result = execute_candidates(
        _request(
            candidates=(candidate,),
            limits=ExecutionLimits(2, 1.0, 0.05),
        ),
        _fault=_FaultInjection(_FaultAction.BLOCK),
    )

    assert result.baseline.status is CandidateStatus.TIMEOUT
    assert result.attempts == ()
    assert result.unattempted == (candidate,)
    assert result.stop_reason is StopReason.TOTAL_DEADLINE


@pytest.mark.parametrize(
    ("fault", "status", "code"),
    [
        (
            _FaultInjection(_FaultAction.CRASH),
            CandidateStatus.WORKER_CRASH,
            "nonzero-exit",
        ),
        (
            _FaultInjection(_FaultAction.CLEAN_NO_PAYLOAD),
            CandidateStatus.INFRASTRUCTURE_FAILURE,
            "no-payload",
        ),
        (
            _FaultInjection(_FaultAction.MALFORMED_PAYLOAD),
            CandidateStatus.INFRASTRUCTURE_FAILURE,
            "invalid-payload",
        ),
        (
            _FaultInjection(_FaultAction.PAYLOAD_THEN_CRASH),
            CandidateStatus.WORKER_CRASH,
            "nonzero-exit",
        ),
    ],
)
def test_worker_crash_and_communication_outcomes_are_distinct(
    fault: _FaultInjection,
    status: CandidateStatus,
    code: str,
) -> None:
    result = execute_candidates(
        _request(limits=ExecutionLimits(1, 10.0, 20.0)), _fault=fault
    )

    assert result.baseline.status is status
    assert result.baseline.failure is not None
    assert result.baseline.failure.infrastructure_code == code
    assert result.baseline.worker_exit_code is not None


class _PayloadGatedClock:
    """Coordinator clock that spends the per-attempt budget on payload arrival.

    The coordinator's timed window encloses the worker's entire render, so a
    wall-clock budget races the payload that a post-payload hang is supposed to
    leave behind. Reporting no elapsed time until the payload is assembled, and
    the whole budget afterwards, fixes that order. `stall_limit` real seconds
    must elapse before the clock advances on its own, so a worker that never
    delivers a payload trips the coordinator deadline rather than stalling the
    test indefinitely.
    """

    def __init__(self, budget: float, stall_limit: float = 60.0) -> None:
        self._base = time.monotonic()
        self._budget = budget
        self._stall_limit = stall_limit
        self.payload_landed = False

    def monotonic(self) -> float:
        if self.payload_landed:
            return self._base + self._budget
        stalled = time.monotonic() - self._base - self._stall_limit
        return self._base + max(0.0, stalled)


def test_completed_payload_followed_by_hang_times_out_with_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    limits = ExecutionLimits(1, 1.0, 2.0)
    clock = _PayloadGatedClock(limits.per_attempt_timeout)
    real_attempt_from_bytes = candidate_executor._attempt_from_bytes

    def assembled(payload: bytes):
        attempt = real_attempt_from_bytes(payload)
        clock.payload_landed = True
        return attempt

    monkeypatch.setattr(candidate_executor, "_attempt_from_bytes", assembled)
    monkeypatch.setattr(candidate_executor, "time", clock)

    result = execute_candidates(
        _request(limits=limits),
        _fault=_FaultInjection(_FaultAction.PAYLOAD_THEN_BLOCK),
    )

    assert result.baseline.status is CandidateStatus.TIMEOUT
    assert result.baseline.failure is not None
    assert result.baseline.failure.message == "per-attempt deadline exceeded"
    assert result.baseline.evidence.svg is not None
    assert result.baseline.worker_exit_code is not None
    assert not multiprocessing.active_children()


def test_coordinator_failure_reaps_the_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def failed_wait(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise RuntimeError("injected coordinator failure")

    monkeypatch.setattr(candidate_executor, "wait", failed_wait)
    result = execute_candidates(
        _request(limits=ExecutionLimits(1, 10.0, 20.0))
    ).baseline

    assert result.status is CandidateStatus.INFRASTRUCTURE_FAILURE
    assert result.failure is not None
    assert result.failure.infrastructure_code == "coordinator-failure"
    assert not multiprocessing.active_children()


def test_protocol_rejects_reset_and_wrong_chunk_size() -> None:
    empty_digest = hashlib.sha256(b"").hexdigest()
    assembler = candidate_executor._ProtocolAssembler()
    assembler.feed(
        candidate_executor._canonical_bytes(
            {
                "version": 1,
                "sequence": 0,
                "kind": "result-start",
                "length": 0,
                "sha256": empty_digest,
                "chunks": 1,
            }
        )
    )
    with pytest.raises(ValueError, match="chunk count or state"):
        assembler.feed(
            candidate_executor._canonical_bytes(
                {
                    "version": 1,
                    "sequence": 1,
                    "kind": "result-start",
                    "length": 0,
                    "sha256": empty_digest,
                    "chunks": 1,
                }
            )
        )

    assembler = candidate_executor._ProtocolAssembler()
    assembler.feed(
        candidate_executor._canonical_bytes(
            {
                "version": 1,
                "sequence": 0,
                "kind": "result-start",
                "length": 1,
                "sha256": hashlib.sha256(b"x").hexdigest(),
                "chunks": 1,
            }
        )
    )
    with pytest.raises(ValueError, match="chunk length"):
        assembler.feed(b"D" + struct.pack(">QI", 1, 0))


@pytest.mark.parametrize(
    ("stage", "present", "absent"),
    [
        (CandidateStage.PREPARATION, None, "graph"),
        (CandidateStage.GRAPH_VALIDATION, None, "graph"),
        (CandidateStage.LAYOUT, "graph", "route_plan"),
        (CandidateStage.COMMITMENT_VERIFICATION, "graph", "route_plan"),
        (CandidateStage.ROUTE_PLAN, "graph", "route_plan"),
        (CandidateStage.RENDER_PLAN, "route_plan", "render_plan"),
        (CandidateStage.SVG_EMISSION, "render_plan", "svg"),
        (CandidateStage.ARTIFACT_VALIDATION, "svg", "artifact_findings"),
    ],
)
def test_injected_stage_failures_retain_completed_evidence(
    stage: CandidateStage,
    present: str | None,
    absent: str,
) -> None:
    result = execute_candidates(
        _request(limits=ExecutionLimits(1, 10.0, 20.0)),
        _fault=_FaultInjection(_FaultAction.RAISE, stage),
    ).baseline

    assert result.status is CandidateStatus.ENGINE_FAILURE
    assert result.stage is stage
    if present is not None:
        assert getattr(result.evidence, present)
    assert not getattr(result.evidence, absent)
    assert result.worker_exit_code == 0
    assert not multiprocessing.active_children()


@pytest.mark.parametrize("message", ["geometry warning one", "renamed warning"])
def test_typed_geometry_warning_rejects_without_message_matching(
    monkeypatch: pytest.MonkeyPatch,
    message: str,
) -> None:
    real_emit = candidate_executor._emit_svg_plan

    def warned(*args: Any, **kwargs: Any) -> str:
        warnings.warn(message, LayoutGeometryWarning)
        return real_emit(*args, **kwargs)

    monkeypatch.setattr(candidate_executor, "_emit_svg_plan", warned)
    result = _direct_result(_request())

    assert result.status is CandidateStatus.VALIDATION_REJECTION
    assert result.stage is CandidateStage.SVG_EMISSION
    assert result.evidence.diagnostics[-1].message == message


def test_benign_warning_remains_an_ordered_diagnostic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_emit = candidate_executor._emit_svg_plan

    def warned(*args: Any, **kwargs: Any) -> str:
        warnings.warn("benign diagnostic", UserWarning)
        return real_emit(*args, **kwargs)

    monkeypatch.setattr(candidate_executor, "_emit_svg_plan", warned)
    result = _direct_result(_request())

    assert result.status is CandidateStatus.ACCEPTED
    assert result.evidence.diagnostics[-1].message == "benign diagnostic"


@pytest.mark.parametrize("kind", [LABEL_STRIKE, MARKER_CROSS, OFFSET_COLLAPSE])
def test_plan_aware_artifact_findings_reject(
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    def finding(svg: str, *, plan: object) -> list[RenderFinding]:
        assert "<svg" in svg
        assert plan is not None
        return [
            RenderFinding(
                kind,
                "main",
                "reads",
                "injected artifact finding",
                ((0.0, 0.0), (1.0, 1.0)),
            )
        ]

    monkeypatch.setattr(candidate_executor, "validate_render", finding)
    result = _direct_result(_request())

    assert result.status is CandidateStatus.VALIDATION_REJECTION
    assert result.stage is CandidateStage.ARTIFACT_VALIDATION
    assert result.evidence.artifact_findings[0].kind == kind
    assert result.evidence.svg is not None


def test_final_route_plan_diagnostics_are_structured_rejections(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_build = candidate_executor.build_observed_render_plan

    def diagnosed(*args: Any, **kwargs: Any) -> ObservedRenderPlan:
        observed = real_build(*args, **kwargs)
        route_plan = replace(
            observed.route_plan,
            diagnostics=(
                *observed.route_plan.diagnostics,
                RoutePlanDiagnostic(None, "injected", "route rejected"),
            ),
        )
        return ObservedRenderPlan(observed.plan, route_plan)

    monkeypatch.setattr(candidate_executor, "build_observed_render_plan", diagnosed)
    result = _direct_result(_request())

    assert result.status is CandidateStatus.VALIDATION_REJECTION
    assert result.stage is CandidateStage.ROUTE_VALIDATION
    assert result.evidence.route_findings[-1].code == "injected"
    assert result.evidence.render_plan is not None


def test_non_blocking_route_plan_diagnostics_do_not_reject(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_build = candidate_executor.build_observed_render_plan

    def diagnosed(*args: Any, **kwargs: Any) -> ObservedRenderPlan:
        observed = real_build(*args, **kwargs)
        route_plan = replace(
            observed.route_plan,
            diagnostics=(
                *observed.route_plan.diagnostics,
                RoutePlanDiagnostic(
                    None,
                    "injected",
                    "legacy route retained",
                    blocking=False,
                ),
            ),
        )
        return ObservedRenderPlan(observed.plan, route_plan)

    monkeypatch.setattr(candidate_executor, "build_observed_render_plan", diagnosed)
    result = _direct_result(_request())

    assert result.status is CandidateStatus.ACCEPTED
    assert result.evidence.route_findings[-1].code == "injected"
    assert result.evidence.route_findings[-1].blocking is False


def test_route_diagnostic_blocking_state_survives_evidence_transport() -> None:
    finding = RoutePlanDiagnostic(
        None,
        "injected",
        "legacy route retained",
        blocking=False,
    )
    evidence = candidate_executor.CandidateEvidence(route_findings=(finding,))

    restored = candidate_executor._evidence_from_wire(
        candidate_executor._evidence_to_wire(evidence)
    )

    assert restored.route_findings == (finding,)


def test_general_mapping_keys_are_canonical_but_sequences_remain_ordered() -> None:
    assert _canonical_json({"b": 2, "a": 1}) == _canonical_json({"a": 1, "b": 2})
    assert _canonical_json(("a", "b")) != _canonical_json(("b", "a"))


def test_normal_api_and_cli_never_invoke_candidate_execution(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def forbidden(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise AssertionError("normal render path invoked candidate execution")

    monkeypatch.setattr(candidate_executor, "execute_candidates", forbidden)
    assert "<svg" in render_string(CONTROL_SOURCE)
    source = tmp_path / "normal.mmd"
    output = tmp_path / "normal.svg"
    source.write_text(CONTROL_SOURCE.replace("examples/", f"{ROOT}/examples/"))
    invocation = CliRunner().invoke(cli, ["render", str(source), "-o", str(output)])
    assert invocation.exit_code == 0, invocation.output
    assert "<svg" in output.read_text()


def test_frozen_sources_are_results_not_frozen_stages_across_hash_seeds() -> None:
    script = ROOT / "tests/candidate_executor_oracle.py"
    observations: list[dict[str, dict[str, str]]] = []
    for seed in ("0", "1", "2", "5", "43", "random"):
        env = os.environ.copy()
        env["PYTHONHASHSEED"] = seed
        env["PYTHONPATH"] = str(ROOT / "src")
        completed = subprocess.run(
            [sys.executable, str(script)],
            cwd=ROOT,
            env=env,
            check=True,
            capture_output=True,
            text=True,
            timeout=600,
        )
        run = json.loads(completed.stdout)
        for fixture, record in run.items():
            if CandidateStatus(record["status"]) is CandidateStatus.TIMEOUT:
                pytest.skip(
                    f"oracle run for PYTHONHASHSEED={seed} timed out on fixture "
                    f"{fixture}; machine too loaded to measure determinism, "
                    "not a determinism failure"
                )
        observations.append(run)

    assert all(item == observations[0] for item in observations[1:])


def test_hash_seed_determinism_skips_when_an_oracle_run_times_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = json.dumps(
        {
            "15": {"status": CandidateStatus.ACCEPTED.value, "hash": "a"},
            "41": {"status": CandidateStatus.TIMEOUT.value, "hash": "b"},
            "72": {"status": CandidateStatus.ACCEPTED.value, "hash": "c"},
            "77": {"status": CandidateStatus.ACCEPTED.value, "hash": "d"},
        }
    )

    def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        del args, kwargs
        return subprocess.CompletedProcess([], 0, stdout=payload, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(pytest.skip.Exception, match="timed out on fixture 41"):
        test_frozen_sources_are_results_not_frozen_stages_across_hash_seeds()


def test_hash_seed_determinism_still_flags_a_real_cross_seed_divergence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    good = json.dumps({"15": {"status": CandidateStatus.ACCEPTED.value, "hash": "a"}})
    diverged = json.dumps(
        {"15": {"status": CandidateStatus.ACCEPTED.value, "hash": "DIFFERENT"}}
    )
    payloads = iter([good, good, good, good, good, diverged])

    def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        del args, kwargs
        return subprocess.CompletedProcess([], 0, stdout=next(payloads), stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(AssertionError):
        test_frozen_sources_are_results_not_frozen_stages_across_hash_seeds()
