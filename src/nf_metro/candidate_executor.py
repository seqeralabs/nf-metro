"""Bounded, policy-free execution of proposed layout commitments.

This module is intentionally absent from normal rendering entry points. It
executes a mandatory baseline and caller-supplied candidates from fresh source,
in fresh spawned workers, and returns evidence without ranking or selecting it.
"""

from __future__ import annotations

import hashlib
import json
import math
import multiprocessing
import os
import struct
import time
import warnings
from collections.abc import Callable, Mapping
from dataclasses import dataclass, fields, is_dataclass, replace
from enum import Enum
from multiprocessing.connection import Connection, wait
from typing import Any, Literal, cast

from nf_metro.api import (
    RenderConfig,
    _emit_svg_plan,
    _prepare_graph_state,
    resolve_theme,
)
from nf_metro.layout import compute_layout
from nf_metro.layout.route_plan import (
    EmissionMemberId,
    RoutePlan,
    RoutePlanDiagnostic,
    build_route_plan_query,
    serialize_route_plan,
)
from nf_metro.layout.routing import compute_station_offsets, observe_route_edges
from nf_metro.options import LAYOUT_OPTIONS, LayoutOption
from nf_metro.parser import ERROR, validate_graph
from nf_metro.parser.commitments import (
    CommitmentConflictError,
    CommitmentSettlementError,
    CommitmentValidationError,
    DirectionCommitment,
    EndpointCommitment,
    GridCommitment,
    LayoutCommitmentOverlay,
    LayoutCommitments,
    verify_settled_commitments,
)
from nf_metro.parser.model import LayoutGeometryWarning, MetroGraph
from nf_metro.parser.validate import ValidationIssue
from nf_metro.render.plan import (
    FrozenMap,
    FrozenRecord,
    RenderPlan,
    freeze_render_value,
)
from nf_metro.render.svg import (
    build_observed_render_plan,
)
from nf_metro.render.validate import RenderFinding, validate_render

__all__ = [
    "CandidateAttemptResult",
    "CandidateExecutionRequest",
    "CandidateExecutionResult",
    "CandidateDiagnostic",
    "CandidateEvidence",
    "CandidateFailure",
    "CandidateStage",
    "CandidateStatus",
    "AttemptKind",
    "CanonicalEvidence",
    "DirectionCommitment",
    "EndpointCommitment",
    "ExecutionLimits",
    "GridCommitment",
    "GraphEvidence",
    "GraphState",
    "LayoutCandidate",
    "LayoutCommitments",
    "LayoutOptionValue",
    "LayoutOptionValues",
    "RenderConfigSnapshot",
    "RenderFinding",
    "RoutePlanDiagnostic",
    "StopReason",
    "ValidationIssue",
    "execute_candidates",
]

LayoutOptionScalar = str | int | float | bool
_CACHE_FIELDS = {
    "_route_topology_query",
    "_linear_entry_pill_lines_cache",
    "_station_lines_cache",
    "_edges_from_cache",
    "_edges_to_cache",
    "_junction_ids_cache",
}
_PROTOCOL_VERSION = 1
_MAX_FRAME_BYTES = 508
_CHUNK_BYTES = _MAX_FRAME_BYTES - 13
_MAX_RESULT_BYTES = 64 * 1024 * 1024
_DRAIN_BATCH = 64


class AttemptKind(str, Enum):
    BASELINE = "baseline"
    CANDIDATE = "candidate"


class CandidateStatus(str, Enum):
    ACCEPTED = "accepted"
    VALIDATION_REJECTION = "validation-rejection"
    ENGINE_FAILURE = "engine-failure"
    TIMEOUT = "timeout"
    WORKER_CRASH = "worker-crash"
    INFRASTRUCTURE_FAILURE = "infrastructure-or-communication-failure"


class CandidateStage(str, Enum):
    REQUEST_VALIDATION = "request-validation"
    PREPARATION = "preparation"
    GRAPH_VALIDATION = "graph-validation"
    LAYOUT = "layout"
    COMMITMENT_VERIFICATION = "commitment-verification"
    ROUTE_PLAN = "route-plan"
    ROUTE_VALIDATION = "route-validation"
    RENDER_PLAN = "render-plan"
    SVG_EMISSION = "svg-emission"
    ARTIFACT_VALIDATION = "artifact-validation"
    COMPLETE = "complete"
    COMMUNICATION = "communication"
    COORDINATOR = "coordinator"


class GraphState(str, Enum):
    RESOLVED = "resolved"
    PARTIAL_LAYOUT = "partial-layout"
    SETTLED = "settled"


class StopReason(str, Enum):
    COMPLETE = "complete"
    ATTEMPT_LIMIT = "attempt-limit"
    TOTAL_DEADLINE = "total-deadline"


@dataclass(frozen=True, slots=True)
class LayoutOptionValue:
    name: str
    value: LayoutOptionScalar


@dataclass(frozen=True, slots=True)
class LayoutOptionValues:
    items: tuple[LayoutOptionValue, ...] = ()


@dataclass(frozen=True, slots=True)
class RenderConfigSnapshot:
    debug: bool = False
    responsive: bool = False
    embed_font: bool = False
    text_to_paths: bool = False
    svg_class_prefix: str = ""
    inject_dark_mode_css: bool = True
    chrome_css: bool = True
    self_color_scheme: bool = True
    baked_mode: Literal["light", "dark"] | None = None
    bare: bool = False


@dataclass(frozen=True, slots=True)
class ExecutionLimits:
    max_attempts: int = 8
    per_attempt_timeout: float = 10.0
    total_deadline: float = 60.0


@dataclass(frozen=True, slots=True)
class LayoutCandidate:
    id: str
    commitments: LayoutCommitments = LayoutCommitments()


@dataclass(frozen=True, slots=True)
class CandidateExecutionRequest:
    source: str
    source_dir: str = ""
    from_nextflow: bool = False
    title: str | None = None
    line_spread: str | None = None
    logo: str | None = None
    legend: str | None = None
    theme: str | None = None
    mode: str | None = None
    layout_options: LayoutOptionValues = LayoutOptionValues()
    caller_pins: LayoutCommitments = LayoutCommitments()
    render: RenderConfigSnapshot = RenderConfigSnapshot()
    candidates: tuple[LayoutCandidate, ...] = ()
    limits: ExecutionLimits = ExecutionLimits()


@dataclass(frozen=True, slots=True)
class CandidateDiagnostic:
    stage: CandidateStage
    category: str
    message: str


@dataclass(frozen=True, slots=True)
class CanonicalEvidence:
    content: str
    sha256: str


@dataclass(frozen=True, slots=True)
class GraphEvidence:
    state: GraphState
    snapshot: CanonicalEvidence


@dataclass(frozen=True, slots=True)
class CandidateEvidence:
    graph: GraphEvidence | None = None
    route_plan: CanonicalEvidence | None = None
    render_plan: CanonicalEvidence | None = None
    svg: CanonicalEvidence | None = None
    graph_findings: tuple[ValidationIssue, ...] = ()
    route_findings: tuple[RoutePlanDiagnostic, ...] = ()
    artifact_findings: tuple[RenderFinding, ...] = ()
    diagnostics: tuple[CandidateDiagnostic, ...] = ()


@dataclass(frozen=True, slots=True)
class CandidateFailure:
    exception_type: str
    message: str
    infrastructure_code: str | None = None


@dataclass(frozen=True, slots=True)
class CandidateAttemptResult:
    index: int
    kind: AttemptKind
    candidate_id: str
    attempted_commitments: LayoutCommitments
    status: CandidateStatus
    stage: CandidateStage
    failure: CandidateFailure | None
    evidence: CandidateEvidence
    worker_exit_code: int | None = None


@dataclass(frozen=True, slots=True)
class CandidateExecutionResult:
    baseline: CandidateAttemptResult
    attempts: tuple[CandidateAttemptResult, ...]
    unattempted: tuple[LayoutCandidate, ...]
    stop_reason: StopReason

    @property
    def attempt_count(self) -> int:
        return 1 + len(self.attempts)


class _FaultAction(str, Enum):
    RAISE = "raise"
    BLOCK = "block"
    CRASH = "crash"
    CLEAN_NO_PAYLOAD = "clean-no-payload"
    MALFORMED_PAYLOAD = "malformed-payload"
    PAYLOAD_THEN_CRASH = "payload-then-crash"
    PAYLOAD_THEN_BLOCK = "payload-then-block"


@dataclass(frozen=True, slots=True)
class _FaultInjection:
    action: _FaultAction
    stage: CandidateStage = CandidateStage.PREPARATION


class _InjectedWorkerError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class _ProductionInput:
    source: str
    source_dir: str
    from_nextflow: bool
    title: str | None
    line_spread: str | None
    logo: str | None
    legend: str | None
    theme: str | None
    mode: str | None
    layout_options: tuple[tuple[str, LayoutOptionScalar], ...]
    caller_pins: LayoutCommitments
    render: RenderConfigSnapshot


@dataclass(frozen=True, slots=True)
class _AttemptInput:
    production: _ProductionInput
    index: int
    kind: AttemptKind
    candidate_id: str
    commitments: LayoutCommitments


def _qualified_type(value: BaseException | type[BaseException]) -> str:
    typ = value if isinstance(value, type) else type(value)
    return f"{typ.__module__}.{typ.__qualname__}"


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode()


def _stable_value(value: Any) -> Any:  # noqa: ANN401
    if isinstance(value, (str, int, float, bool, type(None))):
        return value
    if isinstance(value, Enum):
        return {
            "enum": f"{type(value).__module__}.{type(value).__qualname__}",
            "member": value.name,
        }
    if isinstance(value, FrozenMap):
        entries = [
            [_stable_value(key), _stable_value(item)] for key, item in value.entries
        ]
        entries.sort(key=lambda item: _canonical_bytes(item[0]))
        return {"map": entries}
    if isinstance(value, FrozenRecord):
        return {"record": value.kind, "values": _stable_value(value.values)}
    if is_dataclass(value):
        return {
            "record": f"{type(value).__module__}.{type(value).__qualname__}",
            "values": [
                [field.name, _stable_value(getattr(value, field.name))]
                for field in fields(value)
            ],
        }
    if isinstance(value, Mapping):
        entries = [
            [_stable_value(key), _stable_value(item)] for key, item in value.items()
        ]
        entries.sort(key=lambda item: _canonical_bytes(item[0]))
        return {"map": entries}
    if isinstance(value, (list, tuple)):
        return [_stable_value(item) for item in value]
    if isinstance(value, (set, frozenset)):
        members = [_stable_value(item) for item in value]
        members.sort(key=_canonical_bytes)
        return {"set": members}
    raise TypeError(f"cannot serialise {type(value).__name__}")


def _canonical_json(value: Any) -> str:  # noqa: ANN401
    return _canonical_bytes(_stable_value(value)).decode()


def _canonical_evidence(value: Any) -> CanonicalEvidence:  # noqa: ANN401
    content = _canonical_json(value)
    return CanonicalEvidence(content, hashlib.sha256(content.encode()).hexdigest())


def _freeze_graph(graph: MetroGraph) -> FrozenRecord:
    return FrozenRecord(
        type(graph).__name__,
        FrozenMap(
            tuple(
                (field.name, freeze_render_value(getattr(graph, field.name)))
                for field in fields(graph)
                if field.name not in _CACHE_FIELDS
            )
        ),
    )


def _graph_evidence(graph: MetroGraph, state: GraphState) -> GraphEvidence:
    return GraphEvidence(state, _canonical_evidence(_freeze_graph(graph)))


def _route_evidence(plan: RoutePlan) -> CanonicalEvidence:
    content = serialize_route_plan(plan)
    return CanonicalEvidence(content, hashlib.sha256(content.encode()).hexdigest())


def _failure_result(
    attempt: _AttemptInput,
    status: CandidateStatus,
    stage: CandidateStage,
    exc: BaseException,
    evidence: CandidateEvidence,
    *,
    infrastructure_code: str | None = None,
) -> CandidateAttemptResult:
    return CandidateAttemptResult(
        index=attempt.index,
        kind=attempt.kind,
        candidate_id=attempt.candidate_id,
        attempted_commitments=attempt.commitments,
        status=status,
        stage=stage,
        failure=CandidateFailure(_qualified_type(exc), str(exc), infrastructure_code),
        evidence=evidence,
    )


def _rejection_result(
    attempt: _AttemptInput,
    stage: CandidateStage,
    message: str,
    evidence: CandidateEvidence,
    exception: type[BaseException] = CommitmentValidationError,
) -> CandidateAttemptResult:
    return CandidateAttemptResult(
        index=attempt.index,
        kind=attempt.kind,
        candidate_id=attempt.candidate_id,
        attempted_commitments=attempt.commitments,
        status=CandidateStatus.VALIDATION_REJECTION,
        stage=stage,
        failure=CandidateFailure(_qualified_type(exception), message),
        evidence=evidence,
    )


def _maybe_fault(fault: _FaultInjection | None, stage: CandidateStage) -> None:
    if fault is None or fault.stage is not stage:
        return
    if fault.action is _FaultAction.RAISE:
        raise _InjectedWorkerError(f"injected {stage.value} failure")
    if fault.action is _FaultAction.BLOCK:
        multiprocessing.Event().wait()
    if fault.action is _FaultAction.CRASH:
        os._exit(23)


def _evaluate_attempt(
    attempt: _AttemptInput,
    fault: _FaultInjection | None = None,
    announce: Callable[[CandidateStage], None] | None = None,
) -> CandidateAttemptResult:
    production = attempt.production
    graph_evidence: GraphEvidence | None = None
    route_evidence: CanonicalEvidence | None = None
    render_evidence: CanonicalEvidence | None = None
    svg_evidence: CanonicalEvidence | None = None
    graph_findings: tuple[ValidationIssue, ...] = ()
    route_findings: tuple[RoutePlanDiagnostic, ...] = ()
    artifact_findings: tuple[RenderFinding, ...] = ()
    diagnostics: list[CandidateDiagnostic] = []

    def evidence() -> CandidateEvidence:
        return CandidateEvidence(
            graph=graph_evidence,
            route_plan=route_evidence,
            render_plan=render_evidence,
            svg=svg_evidence,
            graph_findings=graph_findings,
            route_findings=route_findings,
            artifact_findings=artifact_findings,
            diagnostics=tuple(diagnostics),
        )

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        warning_cursor = 0

        def begin(stage: CandidateStage) -> None:
            if announce is not None:
                announce(stage)
            _maybe_fault(fault, stage)

        def collect(stage: CandidateStage) -> bool:
            nonlocal warning_cursor
            new = caught[warning_cursor:]
            warning_cursor = len(caught)
            diagnostics.extend(
                CandidateDiagnostic(
                    stage, _qualified_type(item.category), str(item.message)
                )
                for item in new
            )
            return any(issubclass(item.category, LayoutGeometryWarning) for item in new)

        try:
            begin(CandidateStage.PREPARATION)
            graph = _prepare_graph_state(
                production.source,
                from_nextflow=production.from_nextflow,
                title=production.title,
                line_spread=production.line_spread,
                logo=production.logo,
                legend=production.legend,
                layout_options=dict(production.layout_options),
                source_dir=production.source_dir,
                bare=production.render.bare,
                output_format="svg",
                layout_commitments=LayoutCommitmentOverlay(
                    production.caller_pins, attempt.commitments
                ),
            )
            graph.layout_provenance.validate_complete(graph)
        except (CommitmentValidationError, CommitmentConflictError) as exc:
            collect(CandidateStage.PREPARATION)
            return _rejection_result(
                attempt,
                CandidateStage.PREPARATION,
                str(exc),
                evidence(),
                type(exc),
            )
        except Exception as exc:  # noqa: BLE001
            collect(CandidateStage.PREPARATION)
            return _failure_result(
                attempt,
                CandidateStatus.ENGINE_FAILURE,
                CandidateStage.PREPARATION,
                exc,
                evidence(),
            )
        if collect(CandidateStage.PREPARATION):
            return _rejection_result(
                attempt,
                CandidateStage.PREPARATION,
                "preparation emitted a typed geometry warning",
                evidence(),
            )

        try:
            begin(CandidateStage.GRAPH_VALIDATION)
            issues = validate_graph(graph)
            graph_findings = tuple(issues)
            graph_evidence = _graph_evidence(graph, GraphState.RESOLVED)
        except Exception as exc:  # noqa: BLE001
            collect(CandidateStage.GRAPH_VALIDATION)
            return _failure_result(
                attempt,
                CandidateStatus.ENGINE_FAILURE,
                CandidateStage.GRAPH_VALIDATION,
                exc,
                evidence(),
            )
        if collect(CandidateStage.GRAPH_VALIDATION):
            return _rejection_result(
                attempt,
                CandidateStage.GRAPH_VALIDATION,
                "graph validation emitted a typed geometry warning",
                evidence(),
            )
        if any(item.severity == ERROR for item in issues):
            return _rejection_result(
                attempt,
                CandidateStage.GRAPH_VALIDATION,
                "graph validation rejected the attempt",
                evidence(),
            )

        try:
            begin(CandidateStage.LAYOUT)
            theme = resolve_theme(production.theme, graph, mode=production.mode)
            compute_layout(graph, validate=True, validation_theme=theme)
            graph_evidence = _graph_evidence(graph, GraphState.SETTLED)
        except Exception as exc:  # noqa: BLE001
            graph_evidence = _graph_evidence(graph, GraphState.PARTIAL_LAYOUT)
            collect(CandidateStage.LAYOUT)
            return _failure_result(
                attempt,
                CandidateStatus.ENGINE_FAILURE,
                CandidateStage.LAYOUT,
                exc,
                evidence(),
            )
        if collect(CandidateStage.LAYOUT):
            return _rejection_result(
                attempt,
                CandidateStage.LAYOUT,
                "layout emitted a typed geometry warning",
                evidence(),
            )

        try:
            begin(CandidateStage.COMMITMENT_VERIFICATION)
            verify_settled_commitments(
                graph,
                LayoutCommitmentOverlay(production.caller_pins, attempt.commitments),
            )
        except CommitmentSettlementError as exc:
            collect(CandidateStage.COMMITMENT_VERIFICATION)
            return _rejection_result(
                attempt,
                CandidateStage.COMMITMENT_VERIFICATION,
                str(exc),
                evidence(),
                type(exc),
            )
        except Exception as exc:  # noqa: BLE001
            collect(CandidateStage.COMMITMENT_VERIFICATION)
            return _failure_result(
                attempt,
                CandidateStatus.ENGINE_FAILURE,
                CandidateStage.COMMITMENT_VERIFICATION,
                exc,
                evidence(),
            )
        if collect(CandidateStage.COMMITMENT_VERIFICATION):
            return _rejection_result(
                attempt,
                CandidateStage.COMMITMENT_VERIFICATION,
                "commitment verification emitted a typed geometry warning",
                evidence(),
            )

        try:
            begin(CandidateStage.ROUTE_PLAN)
            offsets = compute_station_offsets(graph)
            observation = observe_route_edges(graph, station_offsets=offsets)
            build_route_plan_query(observation.plan)
            route_evidence = _route_evidence(observation.plan)
            route_findings = observation.plan.diagnostics
        except Exception as exc:  # noqa: BLE001
            collect(CandidateStage.ROUTE_PLAN)
            return _failure_result(
                attempt,
                CandidateStatus.ENGINE_FAILURE,
                CandidateStage.ROUTE_PLAN,
                exc,
                evidence(),
            )
        if collect(CandidateStage.ROUTE_PLAN):
            return _rejection_result(
                attempt,
                CandidateStage.ROUTE_PLAN,
                "routing emitted a typed geometry warning",
                evidence(),
            )
        if any(item.blocking for item in route_findings):
            return _rejection_result(
                attempt,
                CandidateStage.ROUTE_VALIDATION,
                "route-plan validation rejected the attempt",
                evidence(),
            )

        try:
            begin(CandidateStage.RENDER_PLAN)
            observed = build_observed_render_plan(
                graph,
                theme,
                debug=production.render.debug,
                chrome_css=production.render.chrome_css,
                bare=production.render.bare,
            )
            build_route_plan_query(observed.route_plan)
            route_evidence = _route_evidence(observed.route_plan)
            route_findings = observed.route_plan.diagnostics
            render_evidence = _canonical_evidence(observed.plan)
        except Exception as exc:  # noqa: BLE001
            collect(CandidateStage.RENDER_PLAN)
            return _failure_result(
                attempt,
                CandidateStatus.ENGINE_FAILURE,
                CandidateStage.RENDER_PLAN,
                exc,
                evidence(),
            )
        if collect(CandidateStage.RENDER_PLAN):
            return _rejection_result(
                attempt,
                CandidateStage.RENDER_PLAN,
                "render planning emitted a typed geometry warning",
                evidence(),
            )
        if any(item.blocking for item in route_findings):
            return _rejection_result(
                attempt,
                CandidateStage.ROUTE_VALIDATION,
                "final route-plan validation rejected the attempt",
                evidence(),
            )

        plan: RenderPlan = observed.plan
        baked_mode = production.render.baked_mode
        if baked_mode is None:
            baked_mode = cast(
                Literal["light", "dark"] | None,
                (production.mode or graph.mode).strip() or None,
            )
        try:
            begin(CandidateStage.SVG_EMISSION)
            svg = _emit_svg_plan(
                graph,
                plan,
                RenderConfig(
                    debug=production.render.debug,
                    responsive=production.render.responsive,
                    embed_font=production.render.embed_font,
                    text_to_paths=production.render.text_to_paths,
                    svg_class_prefix=production.render.svg_class_prefix,
                    inject_dark_mode_css=production.render.inject_dark_mode_css,
                    chrome_css=production.render.chrome_css,
                    self_color_scheme=production.render.self_color_scheme,
                    baked_mode=baked_mode,
                    bare=production.render.bare,
                ),
            )
            svg_evidence = CanonicalEvidence(
                svg, hashlib.sha256(svg.encode()).hexdigest()
            )
        except Exception as exc:  # noqa: BLE001
            collect(CandidateStage.SVG_EMISSION)
            return _failure_result(
                attempt,
                CandidateStatus.ENGINE_FAILURE,
                CandidateStage.SVG_EMISSION,
                exc,
                evidence(),
            )
        if collect(CandidateStage.SVG_EMISSION):
            return _rejection_result(
                attempt,
                CandidateStage.SVG_EMISSION,
                "SVG emission produced a typed geometry warning",
                evidence(),
            )

        try:
            begin(CandidateStage.ARTIFACT_VALIDATION)
            artifact_findings = tuple(validate_render(svg, plan=plan))
        except Exception as exc:  # noqa: BLE001
            collect(CandidateStage.ARTIFACT_VALIDATION)
            return _failure_result(
                attempt,
                CandidateStatus.ENGINE_FAILURE,
                CandidateStage.ARTIFACT_VALIDATION,
                exc,
                evidence(),
            )
        if collect(CandidateStage.ARTIFACT_VALIDATION):
            return _rejection_result(
                attempt,
                CandidateStage.ARTIFACT_VALIDATION,
                "artifact validation emitted a typed geometry warning",
                evidence(),
            )
        if artifact_findings:
            return _rejection_result(
                attempt,
                CandidateStage.ARTIFACT_VALIDATION,
                "rendered-artifact validation rejected the attempt",
                evidence(),
            )

    return CandidateAttemptResult(
        index=attempt.index,
        kind=attempt.kind,
        candidate_id=attempt.candidate_id,
        attempted_commitments=attempt.commitments,
        status=CandidateStatus.ACCEPTED,
        stage=CandidateStage.COMPLETE,
        failure=None,
        evidence=evidence(),
    )


def _evidence_to_wire(value: CandidateEvidence) -> dict[str, object]:
    return {
        "graph": None
        if value.graph is None
        else {
            "state": value.graph.state.value,
            "snapshot": vars_wire(value.graph.snapshot),
        },
        "route_plan": None if value.route_plan is None else vars_wire(value.route_plan),
        "render_plan": None
        if value.render_plan is None
        else vars_wire(value.render_plan),
        "svg": None if value.svg is None else vars_wire(value.svg),
        "graph_findings": [vars_wire(item) for item in value.graph_findings],
        "route_findings": [vars_wire(item) for item in value.route_findings],
        "artifact_findings": [
            {
                "kind": item.kind,
                "line_id": item.line_id,
                "station_id": item.station_id,
                "message": item.message,
                "segment": [list(point) for point in item.segment],
            }
            for item in value.artifact_findings
        ],
        "diagnostics": [
            {
                "stage": item.stage.value,
                "category": item.category,
                "message": item.message,
            }
            for item in value.diagnostics
        ],
    }


def vars_wire(value: Any) -> dict[str, object]:  # noqa: ANN401
    return {field.name: getattr(value, field.name) for field in fields(value)}


def _attempt_to_bytes(value: CandidateAttemptResult) -> bytes:
    raw = {
        "index": value.index,
        "kind": value.kind.value,
        "candidate_id": value.candidate_id,
        "status": value.status.value,
        "stage": value.stage.value,
        "failure": None if value.failure is None else vars_wire(value.failure),
        "evidence": _evidence_to_wire(value.evidence),
    }
    return _canonical_bytes(raw)


def _canonical_from_wire(value: object) -> CanonicalEvidence:
    raw = _object(value)
    return CanonicalEvidence(str(raw["content"]), str(raw["sha256"]))


def _evidence_from_wire(value: object) -> CandidateEvidence:
    raw = _object(value)
    graph_raw = raw["graph"]
    graph = None
    if graph_raw is not None:
        item = _object(graph_raw)
        graph = GraphEvidence(
            GraphState(str(item["state"])),
            _canonical_from_wire(item["snapshot"]),
        )
    route = (
        _canonical_from_wire(raw["route_plan"])
        if raw["route_plan"] is not None
        else None
    )
    render = (
        _canonical_from_wire(raw["render_plan"])
        if raw["render_plan"] is not None
        else None
    )
    svg = None
    if raw["svg"] is not None:
        svg = _canonical_from_wire(raw["svg"])
    graph_findings = tuple(
        ValidationIssue(
            str(_object(item)["severity"]),
            str(_object(item)["message"]),
            cast(int | None, _object(item)["line"]),
        )
        for item in _array(raw["graph_findings"])
    )
    route_findings = tuple(
        RoutePlanDiagnostic(
            cast(EmissionMemberId | None, _object(item)["member_id"]),
            str(_object(item)["code"]),
            str(_object(item)["message"]),
            bool(_object(item)["blocking"]),
        )
        for item in _array(raw["route_findings"])
    )
    artifact_findings = tuple(
        RenderFinding(
            str(_object(item)["kind"]),
            str(_object(item)["line_id"]),
            str(_object(item)["station_id"]),
            str(_object(item)["message"]),
            cast(
                tuple[tuple[float, float], tuple[float, float]],
                tuple(_point(point) for point in _array(_object(item)["segment"])),
            ),
        )
        for item in _array(raw["artifact_findings"])
    )
    diagnostics = tuple(
        CandidateDiagnostic(
            CandidateStage(str(_object(item)["stage"])),
            str(_object(item)["category"]),
            str(_object(item)["message"]),
        )
        for item in _array(raw["diagnostics"])
    )
    return CandidateEvidence(
        graph=graph,
        route_plan=route,
        render_plan=render,
        svg=svg,
        graph_findings=graph_findings,
        route_findings=route_findings,
        artifact_findings=artifact_findings,
        diagnostics=diagnostics,
    )


def _attempt_from_bytes(value: bytes) -> CandidateAttemptResult:
    raw = _object(json.loads(value))
    failure = None
    if raw["failure"] is not None:
        item = _object(raw["failure"])
        failure = CandidateFailure(
            str(item["exception_type"]),
            str(item["message"]),
            cast(str | None, item["infrastructure_code"]),
        )
    return CandidateAttemptResult(
        index=_integer(raw["index"]),
        kind=AttemptKind(str(raw["kind"])),
        candidate_id=str(raw["candidate_id"]),
        attempted_commitments=LayoutCommitments(),
        status=CandidateStatus(str(raw["status"])),
        stage=CandidateStage(str(raw["stage"])),
        failure=failure,
        evidence=_evidence_from_wire(raw["evidence"]),
    )


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ValueError("wire object is malformed")
    return value


def _array(value: object) -> list[object]:
    if not isinstance(value, list):
        raise ValueError("wire array is malformed")
    return value


def _point(value: object) -> tuple[float, float]:
    coordinates = _array(value)
    if len(coordinates) != 2 or any(
        not isinstance(coordinate, (int, float)) or isinstance(coordinate, bool)
        for coordinate in coordinates
    ):
        raise ValueError("wire point is malformed")
    return (
        float(cast(int | float, coordinates[0])),
        float(cast(int | float, coordinates[1])),
    )


def _integer(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError("wire integer is malformed")
    return value


def _send_control(
    connection: Connection, sequence: int, kind: str, **payload: object
) -> int:
    frame = _canonical_bytes(
        {"version": _PROTOCOL_VERSION, "sequence": sequence, "kind": kind, **payload}
    )
    if len(frame) > _MAX_FRAME_BYTES:
        raise ValueError("control frame exceeds atomic protocol limit")
    connection.send_bytes(frame)
    return sequence + 1


def _send_result(
    connection: Connection, sequence: int, result: CandidateAttemptResult
) -> int:
    payload = _attempt_to_bytes(result)
    if len(payload) > _MAX_RESULT_BYTES:
        raise ValueError("candidate result exceeds protocol payload limit")
    digest = hashlib.sha256(payload).hexdigest()
    chunks = max(1, math.ceil(len(payload) / _CHUNK_BYTES))
    sequence = _send_control(
        connection,
        sequence,
        "result-start",
        length=len(payload),
        sha256=digest,
        chunks=chunks,
    )
    for index in range(chunks):
        chunk = payload[index * _CHUNK_BYTES : (index + 1) * _CHUNK_BYTES]
        frame = b"D" + struct.pack(">QI", sequence, index) + chunk
        if len(frame) > _MAX_FRAME_BYTES:
            raise ValueError("data frame exceeds atomic protocol limit")
        connection.send_bytes(frame)
        sequence += 1
    return _send_control(connection, sequence, "result-end")


def _worker_entry(
    connection: Connection,
    attempt: _AttemptInput,
    fault: _FaultInjection | None,
) -> None:
    sequence = 0
    try:
        if fault is not None and fault.action is _FaultAction.CLEAN_NO_PAYLOAD:
            return
        if fault is not None and fault.action is _FaultAction.MALFORMED_PAYLOAD:
            connection.send_bytes(b"not-json")
            return

        def announce(stage: CandidateStage) -> None:
            nonlocal sequence
            sequence = _send_control(connection, sequence, "stage", stage=stage.value)

        result = _evaluate_attempt(attempt, fault, announce)
        sequence = _send_result(connection, sequence, result)
        if fault is not None and fault.action is _FaultAction.PAYLOAD_THEN_CRASH:
            os._exit(24)
        if fault is not None and fault.action is _FaultAction.PAYLOAD_THEN_BLOCK:
            multiprocessing.Event().wait()
    finally:
        connection.close()


@dataclass(slots=True)
class _ProtocolAssembler:
    sequence: int = 0
    stage: CandidateStage = CandidateStage.COORDINATOR
    expected_length: int | None = None
    expected_digest: str | None = None
    expected_chunks: int | None = None
    chunks: list[bytes] | None = None
    received_length: int = 0
    result: CandidateAttemptResult | None = None

    def feed(self, frame: bytes) -> None:
        if self.result is not None:
            raise ValueError("protocol frame arrived after the completed result")
        if frame.startswith(b"D"):
            if len(frame) < 13 or self.chunks is None:
                raise ValueError("unexpected protocol data frame")
            sequence, index = struct.unpack(">QI", frame[1:13])
            self._accept_sequence(sequence)
            if index != len(self.chunks):
                raise ValueError("out-of-order protocol chunk")
            chunk = frame[13:]
            assert self.expected_length is not None
            expected_size = min(
                _CHUNK_BYTES,
                max(0, self.expected_length - index * _CHUNK_BYTES),
            )
            if len(chunk) != expected_size:
                raise ValueError("protocol chunk length mismatch")
            self.received_length += len(chunk)
            if self.received_length > _MAX_RESULT_BYTES:
                raise ValueError("protocol payload exceeds hard limit")
            self.chunks.append(chunk)
            return

        raw = _object(json.loads(frame))
        if raw.get("version") != _PROTOCOL_VERSION:
            raise ValueError("unsupported protocol version")
        sequence = raw.get("sequence")
        if not isinstance(sequence, int) or isinstance(sequence, bool):
            raise ValueError("protocol sequence is malformed")
        self._accept_sequence(sequence)
        kind = raw.get("kind")
        if kind == "stage":
            if self.expected_length is not None:
                raise ValueError("stage frame interrupted a result payload")
            self.stage = CandidateStage(str(raw["stage"]))
            return
        if kind == "result-start":
            length = raw.get("length")
            chunks = raw.get("chunks")
            digest = raw.get("sha256")
            if (
                not isinstance(length, int)
                or isinstance(length, bool)
                or length < 0
                or length > _MAX_RESULT_BYTES
                or not isinstance(chunks, int)
                or isinstance(chunks, bool)
                or not isinstance(digest, str)
                or len(digest) != 64
                or any(character not in "0123456789abcdef" for character in digest)
            ):
                raise ValueError("result header is malformed")
            exact_chunks = max(1, math.ceil(length / _CHUNK_BYTES))
            if chunks != exact_chunks or self.expected_length is not None:
                raise ValueError("result header has an invalid chunk count or state")
            self.expected_length = length
            self.expected_chunks = chunks
            self.expected_digest = digest
            self.chunks = []
            self.received_length = 0
            return
        if kind == "result-end":
            if (
                self.chunks is None
                or self.expected_length is None
                or self.expected_chunks is None
                or self.expected_digest is None
                or len(self.chunks) != self.expected_chunks
                or self.received_length != self.expected_length
            ):
                raise ValueError("result ended before every chunk arrived")
            payload = b"".join(self.chunks)
            if len(payload) != self.expected_length:
                raise ValueError("result payload length mismatch")
            if hashlib.sha256(payload).hexdigest() != self.expected_digest:
                raise ValueError("result payload digest mismatch")
            self.result = _attempt_from_bytes(payload)
            return
        raise ValueError(f"unknown protocol frame {kind!r}")

    def _accept_sequence(self, sequence: int) -> None:
        if sequence != self.sequence:
            raise ValueError("protocol sequence gap")
        self.sequence += 1


def _infrastructure_result(
    attempt: _AttemptInput,
    stage: CandidateStage,
    code: str,
    message: str,
    *,
    exit_code: int | None = None,
    evidence: CandidateEvidence = CandidateEvidence(),
) -> CandidateAttemptResult:
    return CandidateAttemptResult(
        index=attempt.index,
        kind=attempt.kind,
        candidate_id=attempt.candidate_id,
        attempted_commitments=attempt.commitments,
        status=CandidateStatus.INFRASTRUCTURE_FAILURE,
        stage=stage,
        failure=CandidateFailure("worker.infrastructure", message, code),
        evidence=evidence,
        worker_exit_code=exit_code,
    )


def _bounded_reap(process: multiprocessing.Process, terminate: bool) -> int | None:
    if process.pid is None:
        try:
            process.close()
        except ValueError:
            pass
        return None
    if terminate and process.is_alive():
        process.terminate()
    if process.pid is not None:
        process.join(0.2)
        if process.is_alive():
            process.kill()
            process.join(0.2)
    exit_code = process.exitcode
    try:
        process.close()
    except ValueError:
        pass
    return exit_code


def _timeout_result(
    attempt: _AttemptInput,
    stage: CandidateStage,
    message: str,
    exit_code: int | None,
    completed: CandidateAttemptResult | None,
) -> CandidateAttemptResult:
    evidence = completed.evidence if completed is not None else CandidateEvidence()
    return CandidateAttemptResult(
        index=attempt.index,
        kind=attempt.kind,
        candidate_id=attempt.candidate_id,
        attempted_commitments=attempt.commitments,
        status=CandidateStatus.TIMEOUT,
        stage=stage,
        failure=CandidateFailure(_qualified_type(TimeoutError), message),
        evidence=evidence,
        worker_exit_code=exit_code,
    )


def _crash_result(
    attempt: _AttemptInput,
    stage: CandidateStage,
    exit_code: int,
    completed: CandidateAttemptResult | None,
) -> CandidateAttemptResult:
    return CandidateAttemptResult(
        index=attempt.index,
        kind=attempt.kind,
        candidate_id=attempt.candidate_id,
        attempted_commitments=attempt.commitments,
        status=CandidateStatus.WORKER_CRASH,
        stage=stage,
        failure=CandidateFailure(
            "worker.nonzero-exit",
            f"worker exited with status {exit_code}",
            "nonzero-exit",
        ),
        evidence=(completed.evidence if completed is not None else CandidateEvidence()),
        worker_exit_code=exit_code,
    )


def _run_one(
    context: Any,  # noqa: ANN401
    attempt: _AttemptInput,
    attempt_deadline: float,
    total_deadline: float,
    fault: _FaultInjection | None,
) -> CandidateAttemptResult:
    receiver: Connection | None = None
    sender: Connection | None = None
    process: multiprocessing.Process | None = None
    reaped = False
    assembler = _ProtocolAssembler()
    completed: CandidateAttemptResult | None = None

    def reap(terminate: bool) -> int | None:
        nonlocal reaped
        if process is None:
            return None
        try:
            return _bounded_reap(process, terminate)
        finally:
            reaped = True

    try:
        try:
            receiver, sender = context.Pipe(duplex=False)
            process = context.Process(
                target=_worker_entry,
                args=(sender, attempt, fault),
            )
            process.start()
            sender.close()
            sentinel = process.sentinel
        except BaseException as exc:  # noqa: BLE001
            exit_code = reap(terminate=True)
            return _infrastructure_result(
                attempt,
                CandidateStage.COORDINATOR,
                "spawn-failure",
                f"{_qualified_type(exc)}: {exc}",
                exit_code=exit_code,
            )

        while True:
            deadline = min(attempt_deadline, total_deadline)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                exit_code = reap(terminate=True)
                message = (
                    "total request deadline exceeded"
                    if total_deadline <= attempt_deadline
                    else "per-attempt deadline exceeded"
                )
                return _timeout_result(
                    attempt, assembler.stage, message, exit_code, completed
                )

            ready = wait((receiver, sentinel), timeout=remaining)
            if not ready:
                continue
            pipe_eof = False
            if receiver in ready:
                for _ in range(_DRAIN_BATCH):
                    if time.monotonic() >= deadline or not receiver.poll(0):
                        break
                    try:
                        frame = receiver.recv_bytes(_MAX_FRAME_BYTES)
                        assembler.feed(frame)
                        completed = assembler.result
                    except EOFError:
                        pipe_eof = True
                        break
                    except Exception as exc:  # noqa: BLE001
                        exit_code = reap(terminate=True)
                        return _infrastructure_result(
                            attempt,
                            CandidateStage.COMMUNICATION,
                            "invalid-payload",
                            f"{_qualified_type(exc)}: {exc}",
                            exit_code=exit_code,
                            evidence=(
                                completed.evidence
                                if completed is not None
                                else CandidateEvidence()
                            ),
                        )
            if sentinel not in ready:
                continue
            if not pipe_eof and receiver.poll(0):
                continue
            process.join(min(0.05, remaining))
            exit_code = process.exitcode
            if exit_code is None:
                continue
            process.close()
            reaped = True
            if exit_code != 0:
                return _crash_result(attempt, assembler.stage, exit_code, completed)
            if completed is None:
                return _infrastructure_result(
                    attempt,
                    CandidateStage.COMMUNICATION,
                    "no-payload",
                    "worker exited cleanly without a complete payload",
                    exit_code=exit_code,
                )
            return replace(
                completed,
                index=attempt.index,
                kind=attempt.kind,
                candidate_id=attempt.candidate_id,
                attempted_commitments=attempt.commitments,
                worker_exit_code=exit_code,
            )
    except Exception as exc:  # noqa: BLE001
        exit_code = reap(terminate=True)
        return _infrastructure_result(
            attempt,
            CandidateStage.COORDINATOR,
            "coordinator-failure",
            f"{_qualified_type(exc)}: {exc}",
            exit_code=exit_code,
            evidence=(
                completed.evidence if completed is not None else CandidateEvidence()
            ),
        )
    finally:
        if receiver is not None:
            receiver.close()
        if sender is not None and not sender.closed:
            sender.close()
        if process is not None and not reaped:
            reap(terminate=True)


def _validate_option_value(option: LayoutOption, value: object) -> bool:
    if option.kind == "bool":
        return isinstance(value, bool)
    if option.kind == "int":
        valid = isinstance(value, int) and not isinstance(value, bool)
    elif option.kind == "float":
        valid = (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(float(value))
        )
    elif option.kind == "choice":
        return isinstance(value, str) and value in option.choices
    else:
        return isinstance(value, str)
    if not valid:
        return False
    number = float(cast(int | float, value))
    if option.sign == "positive" and number <= 0:
        return False
    if option.sign == "nonneg" and number < 0:
        return False
    return option.max_val is None or number <= option.max_val


def _layout_options(
    values: LayoutOptionValues,
) -> tuple[tuple[str, LayoutOptionScalar], ...]:
    if not isinstance(values.items, tuple):
        raise ValueError("layout option values must be an immutable tuple")
    registry = {option.name: option for option in LAYOUT_OPTIONS}
    seen: set[str] = set()
    result: list[tuple[str, LayoutOptionScalar]] = []
    for item in values.items:
        if not isinstance(item, LayoutOptionValue) or not isinstance(item.name, str):
            raise ValueError("layout option record is malformed")
        if item.name in seen:
            raise ValueError(f"duplicate caller layout option {item.name!r}")
        seen.add(item.name)
        option = registry.get(item.name)
        if option is None:
            raise ValueError(f"unknown caller layout option {item.name!r}")
        if not _validate_option_value(option, item.value):
            raise ValueError(
                f"invalid caller layout option {item.name!r}: {item.value!r}"
            )
        result.append((item.name, item.value))
    return tuple(result)


def _validate_request(
    request: CandidateExecutionRequest,
) -> tuple[tuple[str, LayoutOptionScalar], ...]:
    if not isinstance(request, CandidateExecutionRequest):
        raise ValueError("candidate execution request is malformed")
    if not isinstance(request.source, str) or not isinstance(request.source_dir, str):
        raise ValueError("candidate source and source directory must be strings")
    if not isinstance(request.from_nextflow, bool):
        raise ValueError("from_nextflow must be a boolean")
    for name, value in (
        ("title", request.title),
        ("line_spread", request.line_spread),
        ("logo", request.logo),
        ("legend", request.legend),
        ("theme", request.theme),
        ("mode", request.mode),
    ):
        if value is not None and not isinstance(value, str):
            raise ValueError(f"{name} must be a string or None")
    if not isinstance(request.layout_options, LayoutOptionValues):
        raise ValueError("layout options are malformed")
    if not isinstance(request.caller_pins, LayoutCommitments):
        raise ValueError("caller commitments are malformed")
    if not isinstance(request.render, RenderConfigSnapshot):
        raise ValueError("render configuration is malformed")
    for name in (
        "debug",
        "responsive",
        "embed_font",
        "text_to_paths",
        "inject_dark_mode_css",
        "chrome_css",
        "self_color_scheme",
        "bare",
    ):
        if not isinstance(getattr(request.render, name), bool):
            raise ValueError(f"render {name} must be a boolean")
    if not isinstance(request.render.svg_class_prefix, str):
        raise ValueError("render svg_class_prefix must be a string")
    if request.render.baked_mode not in (None, "light", "dark"):
        raise ValueError("render baked_mode must be light, dark, or None")
    if not isinstance(request.candidates, tuple):
        raise ValueError("candidates must be an immutable tuple")
    if not isinstance(request.limits, ExecutionLimits):
        raise ValueError("execution limits are malformed")
    limits = request.limits
    if (
        not isinstance(limits.max_attempts, int)
        or isinstance(limits.max_attempts, bool)
        or limits.max_attempts < 1
    ):
        raise ValueError("max_attempts must include the mandatory baseline")
    for label, timeout_value in (
        ("per_attempt_timeout", limits.per_attempt_timeout),
        ("total_deadline", limits.total_deadline),
    ):
        if not isinstance(timeout_value, (int, float)) or isinstance(
            timeout_value, bool
        ):
            raise ValueError(f"{label} must be a finite positive number")
        if not math.isfinite(float(timeout_value)) or timeout_value <= 0:
            raise ValueError(f"{label} must be a finite positive number")
    ids: set[str] = set()
    for candidate in request.candidates:
        if not isinstance(candidate, LayoutCandidate):
            raise ValueError("candidate record is malformed")
        if not isinstance(candidate.id, str) or not candidate.id:
            raise ValueError("candidate ids must be non-empty")
        if not isinstance(candidate.commitments, LayoutCommitments):
            raise ValueError(f"candidate {candidate.id!r} commitments are malformed")
        if candidate.id in ids:
            raise ValueError(f"duplicate candidate id {candidate.id!r}")
        ids.add(candidate.id)
    return _layout_options(request.layout_options)


def _production_input(
    request: CandidateExecutionRequest,
    layout_options: tuple[tuple[str, LayoutOptionScalar], ...],
) -> _ProductionInput:
    return _ProductionInput(
        source=request.source,
        source_dir=request.source_dir,
        from_nextflow=request.from_nextflow,
        title=request.title,
        line_spread=request.line_spread,
        logo=request.logo,
        legend=request.legend,
        theme=request.theme,
        mode=request.mode,
        layout_options=layout_options,
        caller_pins=request.caller_pins,
        render=request.render,
    )


def _attempt_input(
    production: _ProductionInput,
    index: int,
    candidate: LayoutCandidate | None,
) -> _AttemptInput:
    return _AttemptInput(
        production=production,
        index=index,
        kind=AttemptKind.BASELINE if candidate is None else AttemptKind.CANDIDATE,
        candidate_id="" if candidate is None else candidate.id,
        commitments=LayoutCommitments() if candidate is None else candidate.commitments,
    )


def execute_candidates(
    request: CandidateExecutionRequest,
    *,
    _fault: _FaultInjection | None = None,
) -> CandidateExecutionResult:
    """Execute baseline first, then bounded candidates without selecting one."""
    layout_options = _validate_request(request)
    production = _production_input(request, layout_options)
    context = multiprocessing.get_context("spawn")
    started = time.monotonic()
    total_deadline = started + request.limits.total_deadline
    candidates = request.candidates[: max(0, request.limits.max_attempts - 1)]
    limit_unattempted = request.candidates[len(candidates) :]

    def run(index: int, candidate: LayoutCandidate | None) -> CandidateAttemptResult:
        attempt = _attempt_input(production, index, candidate)
        return _run_one(
            context,
            attempt,
            time.monotonic() + request.limits.per_attempt_timeout,
            total_deadline,
            _fault,
        )

    baseline = run(0, None)
    attempts: list[CandidateAttemptResult] = []
    deadline_unattempted: tuple[LayoutCandidate, ...] = ()
    for offset, candidate in enumerate(candidates, start=1):
        if time.monotonic() >= total_deadline:
            deadline_unattempted = candidates[offset - 1 :]
            break
        attempts.append(run(offset, candidate))

    if deadline_unattempted:
        stop_reason = StopReason.TOTAL_DEADLINE
    elif limit_unattempted:
        stop_reason = StopReason.ATTEMPT_LIMIT
    else:
        stop_reason = StopReason.COMPLETE
    return CandidateExecutionResult(
        baseline=baseline,
        attempts=tuple(attempts),
        unattempted=tuple(deadline_unattempted) + tuple(limit_unattempted),
        stop_reason=stop_reason,
    )
