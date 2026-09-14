"""Final layout guards must inspect the settled geometry the renderer draws.

Geometric bypass and render-time route-envelope settlement can move sections,
ports, or routes after an earlier layout checkpoint. A validated layout defers
affected guards until those transformations finish so validation and rendering
inspect the same geometry. This module covers both the post-bypass ``after
final`` checkpoint (#1339) and route-dependent final guards deferred until
render-plan settlement (#1759).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from nf_metro.layout import engine
from nf_metro.layout.fan_plans import FanRouteInvariantError
from nf_metro.layout.phases.guards import (
    FoldThresholdError,
    LayoutInvariantError,
    PhaseInvariantError,
)
from nf_metro.layout.routing.convergences import ConvergenceInvariantError
from nf_metro.layout.routing.exit_turns import ExitTurnInvariantError
from nf_metro.layout.routing.invariants import CurveInvariantError
from nf_metro.parser.mermaid import parse_metro_mermaid
from nf_metro.render.section_header import SectionHeaderClashError

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"

# Fixtures whose geometric-bypass pass keeps helpers and re-lays the graph, so
# the pre-bypass and settled section geometries genuinely differ.  These are
# the cases that expose a pre-final guard checkpoint; ``fold_bypass_creep`` is
# the sharp one (its ``report`` section drops ~44px between the two states).
_BYPASS_RELAY_FIXTURES = [
    "topologies/fold_bypass_creep.mmd",
    "topologies/fold_bypass_creep_tight.mmd",
    "topologies/inrow_skip_breeze.mmd",
    "topologies/rowmate_tb_side_entry_top_align_grow.mmd",
    "topologies/tb_fork_lane_transpose.mmd",
]


def _section_geometry(graph) -> dict[str, tuple[float, float, float, float]]:
    return {
        s.id: (
            round(s.bbox_x, 1),
            round(s.bbox_y, 1),
            round(s.bbox_x + s.bbox_w, 1),
            round(s.bbox_y + s.bbox_h, 1),
        )
        for s in graph.sections.values()
    }


@pytest.mark.parametrize("fixture", _BYPASS_RELAY_FIXTURES)
def test_after_final_checkpoint_sees_settled_geometry(fixture, monkeypatch) -> None:
    src = (EXAMPLES / fixture).read_text()

    observed: list[dict[str, tuple[float, float, float, float]]] = []
    real_run = engine.run_validate_guards

    def spy(graph, phase, **kwargs):
        if phase == "after final":
            observed.append(_section_geometry(graph))
        return real_run(graph, phase, **kwargs)

    monkeypatch.setattr(engine, "run_validate_guards", spy)

    graph = parse_metro_mermaid(src)
    engine.compute_layout(graph, validate=True)
    settled = _section_geometry(graph)

    assert observed, f"{fixture}: no 'after final' guard checkpoint ran"
    for i, snapshot in enumerate(observed):
        drift = {
            sid: (snapshot[sid], settled[sid])
            for sid in snapshot
            if snapshot[sid] != settled[sid]
        }
        assert not drift, (
            f"{fixture}: 'after final' checkpoint #{i} validated a pre-final "
            f"layout state the renderer never draws: {drift}"
        )


def _deferred_route_guard_harness(monkeypatch, *, render_plan) -> object:
    graph = parse_metro_mermaid(
        "%%metro line: x | X | #ff0000\ngraph LR\n    a[A] -->|x| b[B]\n"
    )

    def defer_route_guards(graph, **_kwargs) -> None:
        graph._final_route_guards_deferred = True

    monkeypatch.setattr(engine, "_compute_layout_scaled", defer_route_guards)
    monkeypatch.setattr(
        "nf_metro.layout.geometric_bypass.apply_geometric_bypass",
        lambda _graph, _layout_pass: False,
    )
    monkeypatch.setattr("nf_metro.render.svg.build_render_plan", render_plan)
    return graph


def test_validated_layout_runs_deferred_route_guards_at_settled_chokepoint(
    monkeypatch,
) -> None:
    calls: list[object] = []

    def capture(graph, theme) -> None:
        calls.append((graph, theme))

    graph = _deferred_route_guard_harness(monkeypatch, render_plan=capture)

    engine.compute_layout(graph, validate=True)

    assert calls and calls[0][0] is graph
    assert graph._final_route_guards_deferred is False


def test_validated_layout_uses_caller_selected_theme_for_deferred_route_guards(
    monkeypatch,
) -> None:
    from nf_metro.themes import resolve_theme

    seen: list[object] = []

    def capture(_graph, theme) -> None:
        seen.append(theme)

    graph = _deferred_route_guard_harness(monkeypatch, render_plan=capture)
    theme = resolve_theme("seqera", graph, mode="light")

    engine.compute_layout(graph, validate=True, validation_theme=theme)

    assert seen == [theme]


def test_unvalidated_layout_does_not_run_deferred_route_guards(monkeypatch) -> None:
    def reject(*_args, **_kwargs) -> None:
        raise AssertionError("unvalidated layout entered route validation")

    graph = _deferred_route_guard_harness(monkeypatch, render_plan=reject)

    engine.compute_layout(graph, validate=False)


def test_deferred_route_guard_failure_propagates_from_validated_layout(
    monkeypatch,
) -> None:
    failure = RuntimeError("settled route validation failed")

    def reject(*_args, **_kwargs) -> None:
        raise failure

    graph = _deferred_route_guard_harness(monkeypatch, render_plan=reject)

    with pytest.raises(RuntimeError, match="settled route validation failed") as raised:
        engine.compute_layout(graph, validate=True)

    assert raised.value is failure


@pytest.mark.parametrize(
    "error_type",
    [
        ConvergenceInvariantError,
        CurveInvariantError,
        ExitTurnInvariantError,
        FanRouteInvariantError,
        SectionHeaderClashError,
    ],
)
def test_deferred_route_guard_invariant_has_layout_boundary(
    monkeypatch, error_type
) -> None:
    failure = error_type("settled route validation failed")

    def reject(*_args, **_kwargs) -> None:
        raise failure

    graph = _deferred_route_guard_harness(monkeypatch, render_plan=reject)

    with pytest.raises(engine.SettledRouteValidationError) as raised:
        engine.compute_layout(graph, validate=True)

    assert raised.value.__cause__ is failure
    assert str(raised.value) == str(failure)
    assert graph._final_route_guards_deferred is False


@pytest.mark.parametrize(
    "failure",
    [
        RuntimeError("unrelated failure"),
        PhaseInvariantError("existing phase failure"),
        LayoutInvariantError("existing layout failure"),
        FoldThresholdError("fold threshold failure"),
    ],
)
def test_deferred_route_guard_other_failures_retain_their_types(
    monkeypatch, failure: Exception
) -> None:
    def reject(*_args, **_kwargs) -> None:
        raise failure

    graph = _deferred_route_guard_harness(monkeypatch, render_plan=reject)

    with pytest.raises(type(failure)) as raised:
        engine.compute_layout(graph, validate=True)

    assert raised.value is failure
    assert graph._final_route_guards_deferred is False


def test_discovery_reservations_defer_route_guards_without_clearance(
    monkeypatch,
) -> None:
    import nf_metro.layout.routing as routing
    from nf_metro.layout.phases import guards

    graph = parse_metro_mermaid(
        "%%metro line: x | X | #ff0000\ngraph LR\n    a[A] -->|x| b[B]\n"
    )
    discovery_routes = [object()]
    discovery = SimpleNamespace(
        routes=discovery_routes,
        plan=SimpleNamespace(
            boundary_clearance_requirements=(),
            reservations=(object(),),
        ),
    )
    monkeypatch.setattr(
        routing, "observe_route_edges", lambda *_args, **_kwargs: discovery
    )

    _offsets, routes = guards._ensure_pass_c_inputs(
        graph,
        {},
        None,
        validate_final_geometry=True,
    )

    assert routes is None
    assert graph._final_route_guards_deferred is True


def test_discovery_without_settlement_intent_validates_routes_immediately(
    monkeypatch,
) -> None:
    import nf_metro.layout.routing as routing
    from nf_metro.layout.phases import guards

    graph = parse_metro_mermaid(
        "%%metro line: x | X | #ff0000\ngraph LR\n    a[A] -->|x| b[B]\n"
    )
    discovery_routes = [object()]
    discovery = SimpleNamespace(
        routes=discovery_routes,
        plan=SimpleNamespace(boundary_clearance_requirements=(), reservations=()),
    )
    monkeypatch.setattr(
        routing, "observe_route_edges", lambda *_args, **_kwargs: discovery
    )

    _offsets, routes = guards._ensure_pass_c_inputs(
        graph,
        {},
        None,
        validate_final_geometry=True,
    )

    assert routes is discovery_routes
    assert graph._final_route_guards_deferred is False


def test_render_consumes_discovery_deferral_at_final_route_guard(
    monkeypatch,
) -> None:
    from nf_metro.render import svg as render_svg
    from nf_metro.themes import resolve_theme

    graph = parse_metro_mermaid(
        "%%metro line: x | X | #ff0000\ngraph LR\n    a[A] -->|x| b[B]\n"
    )
    engine.compute_layout(graph, validate=False)
    graph._validate_active = True
    graph._final_route_guards_deferred = True

    def reject(*_args, **kwargs) -> None:
        assert kwargs["include_deferred_final"] is True
        assert kwargs["strict"] is True
        raise RuntimeError("post-cohort route guard failed")

    monkeypatch.setattr(render_svg, "assert_render_layout_invariants", reject)

    with pytest.raises(RuntimeError, match="post-cohort route guard failed"):
        render_svg._settled_render_graph(graph, resolve_theme(None, graph))
