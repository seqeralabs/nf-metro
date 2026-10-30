"""A settlement re-route draws the observation that compiles its cohort plan.

Envelope settlement re-routes against the ledger it consumed, and the
corridor-cohort compile is one more observation of those same inputs.  Where
that observation accepts a plan and owes no boundary clearance, it is the
geometry the render draws and the plan it publishes carries the accepted record.
"""

from __future__ import annotations

import warnings
from dataclasses import replace
from pathlib import Path

import pytest

import nf_metro.render.svg as svg
from nf_metro.api import prepare_graph, render_string, resolve_theme
from nf_metro.layout.route_plan import RoutePlan, RoutePlanDiagnostic
from nf_metro.layout.routing.common import RoutedPath

ROOT = Path(__file__).parents[1]

# The prior-disposition exit-turn rebuild reads station offsets, so it has to
# start from the offsets the observation was handed, as the other rebuilds do.
# These two maps settle a re-route that otherwise sees offsets the provisional
# build already moved, and abort on a re-route that differs from the pass it
# settled.
OFFSET_REBUILD_REPROS = {
    "four_line_bypass": """\
%%metro line: l0 | Assemble | #e6194b
%%metro line: l1 | Polish | #3cb44b
%%metro line: l2 | Stats | #4363d8
%%metro line: l3 | Trim | #f58231
graph LR
    subgraph sec0 [Quant 0]
        s0n1 -->|l2| s0n2
    end
    subgraph sec1 [Filter_Call 1]
        s1n0 -->|l0| s1n1
        s1n2 -->|l0| s1n3
        s1n3 -->|l3| s1n4
    end
    subgraph sec2 [Sort_Polish_Pca 2]
    end
    s0n2 -->|l0,l3| s1n0
    s1n4 -->|l1| s2n0
    s0n1 -->|l1| s1n4
    s0n2 -->|l2| s2n0
""",
    "four_line_reentry": """\
%%metro line: l0 | Assemble | #e6194b
%%metro line: l1 | Polish | #3cb44b
%%metro line: l2 | Stats | #4363d8
%%metro line: l3 | Trim | #f58231
graph LR
    subgraph sec0 [Quant 0]
        s0n0 -->|l1,l2| s0n1
        s0n1 -->|l0,l2| s0n2
        s0n0 -->|l3| s0n2
    end
    subgraph sec1 [Filter_Call 1]
        s1n0 -->|l0| s1n1
        s1n1 -->|l0| s1n2
        s1n2 -->|l0| s1n3
        s1n3 -->|l3| s1n4
        s1n0 -->|l3| s1n3
    end
    subgraph sec2 [Sort_Polish_Pca 2]
    end
    s0n2 -->|l0,l3| s1n0
    s1n4 -->|l0,l3| s2n0
    s1n1 -->|l0| s2n1
    s0n1 -->|l1| s1n4
    s0n2 -->|l2| s2n0
    s0n2 -->|l3| s2n1
""",
}

# Settlement moves nothing here but changes a live-derived band, so the map is
# re-routed at zero translation: that re-route and the compiling observation
# both replay the inputs of the first pass.
ZERO_TRANSLATION_REROUTE = """\
%%metro line: l0 | Merge_Annotate_Export | #e6194b
%%metro line: l1 | Stats_Filter | #3cb44b
%%metro line: l2 | Vcf_Align_Cluster | #4363d8
%%metro grid: sec0 | 0,0
%%metro grid: sec1 | 1,0
%%metro grid: sec2 | 2,0

graph LR
    subgraph sec0 [Bin 0]
        s0n0[count]
        s0n1[quant]
        s0n2[bam_classify]
        s0n3[cluster]
        s0n0 -->|l0| s0n2
        s0n1 -->|l1,l2| s0n2
        s0n2 -->|l1,l2| s0n3
        s0n0 -->|l2| s0n1
    end
    subgraph sec1 [Call 1]
        s1n0[classify_classify]
        s1n1[summarise]
        s1n2[trim_normalize]
        s1n0 -->|l2| s1n1
        s1n1 -->|l2| s1n2
    end
    subgraph sec2 [Bam_Pca 2]
        s2n0[classify_index_trim]
        s2n1[markdup_markdup_quant]
        s2n3[bin]
        s2n0 -->|l1,l2| s2n1
        s2n1 -->|l2| s2n3
    end
    s0n2 -->|l0| s1n2
    s0n2 -->|l1| s2n0
    s0n3 -->|l1| s2n1
    s0n3 -->|l2| s1n0
    s1n2 -->|l2| s2n0
"""


def _render(text: str) -> str:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return render_string(text)


def _points_by_edge(
    routes: list[RoutedPath],
) -> dict[tuple[str, str, str], list[tuple[float, float]]]:
    return {
        (route.edge.source, route.edge.target, route.line_id): [
            (round(x, 6), round(y, 6)) for x, y in route.points
        ]
        for route in routes
    }


@pytest.mark.parametrize("name", sorted(OFFSET_REBUILD_REPROS))
def test_a_prior_disposition_rebuild_renders(name: str) -> None:
    assert _render(OFFSET_REBUILD_REPROS[name]).lstrip().startswith("<")


def test_a_zero_translation_reroute_replays_the_first_pass_point_for_point(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observations: list[tuple[RoutePlan | None, dict]] = []
    settled: list[svg._SettledRenderGeometry] = []
    real_observe = svg.observe_route_edges_centred
    real_settle = svg._settle_render_geometry

    def observe(graph, **kwargs):
        observation = real_observe(graph, **kwargs)
        observations.append(
            (kwargs.get("reservations"), _points_by_edge(observation.routes))
        )
        return observation

    def settle(*args, **kwargs):
        observations.clear()
        result = real_settle(*args, **kwargs)
        settled.append(result)
        return result

    monkeypatch.setattr(svg, "observe_route_edges_centred", observe)
    monkeypatch.setattr(svg, "_settle_render_geometry", settle)
    _render(ZERO_TRANSLATION_REROUTE)

    assert not settled[-1].settlement.translations
    (first_ledger, first), (reroute_ledger, reroute), (_, compiling) = observations
    assert first_ledger is None and reroute_ledger is not None
    assert settled[-1].route_plan.accepted_corridor_cohort_plan is not None
    assert first
    assert reroute == first
    assert compiling == first
    assert _points_by_edge(settled[-1].routes) == first


def test_a_settled_fan_out_publishes_the_plan_its_drawn_observation_accepted() -> None:
    path = ROOT / "examples" / "guide" / "03_fan_out.mmd"
    graph = prepare_graph(path.read_text(), source_dir=str(path.parent))
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        observed = svg.build_observed_render_plan(graph, resolve_theme(None, graph))

    assert observed.route_plan is not None
    assert observed.route_plan.accepted_corridor_cohort_plan is not None


def test_a_drawn_observation_publishes_the_disposition_overrides_of_both_reroutes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No repository map overrides a fresh disposition on a re-route, so the
    overrides are reported synthetically by every re-route that consumes a
    ledger: the settlement re-route and the compiling observation drawn after it.
    """
    real_observe = svg.observe_route_edges_centred
    reported: list[RoutePlanDiagnostic] = []
    published: list[RoutePlan] = []
    real_settle = svg._settle_render_geometry

    def observe(graph, **kwargs):
        observation = real_observe(graph, **kwargs)
        if kwargs.get("reservations") is not None:
            override = RoutePlanDiagnostic(
                None,
                "exit-turn-disposition-adopted",
                f"re-route {len(reported)}",
                blocking=False,
            )
            reported.append(override)
            observation.plan = replace(
                observation.plan,
                diagnostics=observation.plan.diagnostics + (override,),
            )
        return observation

    def settle(*args, **kwargs):
        reported.clear()
        result = real_settle(*args, **kwargs)
        published.append(result.route_plan)
        return result

    monkeypatch.setattr(svg, "observe_route_edges_centred", observe)
    monkeypatch.setattr(svg, "_settle_render_geometry", settle)
    _render(ZERO_TRANSLATION_REROUTE)

    assert len(reported) == 2
    carried = [
        item
        for item in published[-1].diagnostics
        if item.code == "exit-turn-disposition-adopted"
    ]
    assert carried == reported
