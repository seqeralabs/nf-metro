"""A render draws the observation that compiles its cohort plan.

The corridor-cohort compile is one more observation of the inputs the last
routing pass read, including the reserved bands that pass seated corridors in.
Where that observation accepts a plan and owes no boundary clearance, it is the
geometry the render draws and the plan it publishes carries the accepted record,
whether or not envelope settlement re-routed before it.
"""

from __future__ import annotations

import warnings
from dataclasses import replace
from pathlib import Path
from typing import NamedTuple

import pytest

import nf_metro.layout.routing.core as routing_core
import nf_metro.render.svg as svg
from nf_metro.api import prepare_graph, render_string, resolve_theme
from nf_metro.layout.route_plan import RoutePlan, RoutePlanDiagnostic
from nf_metro.layout.routing.common import RoutedPath
from nf_metro.layout.routing.reserved_bands import ReservedCorridors

ROOT = Path(__file__).parents[1]

# The prior-disposition exit-turn rebuild must start from the offsets the
# observation was handed; from offsets the provisional build already moved, the
# re-route differs from the pass it settles.
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


# Maps no envelope settlement re-routes: the only observations consuming a ledger
# are the compiling one and, for a convergence grant, its ungranted replay.
UNSETTLED_MAPS = {
    "01_minimal": ROOT / "examples" / "guide" / "01_minimal.mmd",
    "02_sections": ROOT / "examples" / "guide" / "02_sections.mmd",
    "07_track_gap": ROOT / "examples" / "guide" / "07_track_gap.mmd",
    "genomeassembly": ROOT / "examples" / "genomeassembly.mmd",
}

# Unsettled, and its compile grants a convergence corridor that moves a trunk.
UNSETTLED_CONVERGENCE_GRANT = """\
%%metro line: m0 | L0 | #e6194b
%%metro line: m1 | L1 | #3cb44b
%%metro line: m2 | L2 | #4363d8
%%metro line: m3 | L3 | #f58231

graph LR
    subgraph z0 [Zone 0]
        q0x0[Q0X0]
        q0x1[Q0X1]
        q0x2[Q0X2]
        q0x0 -->|m2| q0x1
        q0x0 -->|m0| q0x2
        q0x1 -->|m2| q0x2
    end
    subgraph z1 [Zone 1]
        %%metro direction: BT
        q1x0[Q1X0]
        q1x1[Q1X1]
        q1x0 -->|m0,m1| q1x1
    end
    subgraph z2 [Zone 2]
        %%metro direction: TB
        q2x0[Q2X0]
        q2x1[Q2X1]
        q2x0 -->|m2,m3| q2x1
    end
    subgraph z3 [Zone 3]
        %%metro direction: BT
        q3x0[Q3X0]
        q3x1[Q3X1]
        q3x0 -->|m2,m3| q3x1
    end
    q0x0 -->|m1,m3| q1x1
    q0x2 -->|m0,m1,m3| q1x0
    q0x2 -->|m2| q2x0
    q1x0 -->|m3| q3x1
    q1x1 -->|m0,m2,m3| q2x0
    q2x1 -->|m2,m3| q3x0
"""


class _Observed(NamedTuple):
    consumes_ledger: bool
    compiles: bool
    points: dict[tuple[str, str, str], list[tuple[float, float]]]
    plan: RoutePlan
    bands: ReservedCorridors | None


def _observe_last_settle(
    monkeypatch: pytest.MonkeyPatch, text: str, source_dir: Path
) -> tuple[list[_Observed], svg._SettledRenderGeometry]:
    """Render *text*; return the last settle call's observations and result."""
    observed: list[_Observed] = []
    bands_read: list[ReservedCorridors] = []
    settled: list[svg._SettledRenderGeometry] = []
    real_observe = svg.observe_route_edges_centred
    real_context = routing_core._build_routing_context
    real_settle = svg._settle_render_geometry

    def context(*args, **kwargs):
        bands = kwargs.get("reserved_bands")
        bands_read.append(ReservedCorridors() if bands is None else bands)
        return real_context(*args, **kwargs)

    def observe(graph, **kwargs):
        bands_read.clear()
        observation = real_observe(graph, **kwargs)
        observed.append(
            _Observed(
                kwargs.get("reservations") is not None,
                kwargs.get("allow_convergence_clearance_requirements", False),
                _points_by_edge(observation.routes),
                observation.plan,
                bands_read[0] if len(bands_read) == 1 else None,
            )
        )
        return observation

    def settle(*args, **kwargs):
        observed.clear()
        result = real_settle(*args, **kwargs)
        settled.append(result)
        return result

    monkeypatch.setattr(routing_core, "_build_routing_context", context)
    monkeypatch.setattr(svg, "observe_route_edges_centred", observe)
    monkeypatch.setattr(svg, "_settle_render_geometry", settle)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        render_string(text, source_dir=str(source_dir))
    return observed, settled[-1]


def _compiling_index(observed: list[_Observed]) -> int:
    return next(
        index
        for index, item in enumerate(observed)
        if item.consumes_ledger and item.compiles
    )


@pytest.mark.parametrize("name", sorted(UNSETTLED_MAPS))
def test_an_unsettled_render_draws_its_compiling_observation(
    name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = UNSETTLED_MAPS[name]
    observed, settled = _observe_last_settle(monkeypatch, path.read_text(), path.parent)

    compiling = _compiling_index(observed)
    assert not any(item.consumes_ledger for item in observed[:compiling])
    observation = observed[compiling]
    accepted = observation.plan.accepted_corridor_cohort_plan
    assert accepted is not None
    assert not observation.plan.boundary_clearance_requirements

    assert _points_by_edge(settled.routes) == observation.points
    assert settled.route_plan.accepted_corridor_cohort_plan == accepted


@pytest.mark.parametrize(
    "name", [*sorted(UNSETTLED_MAPS), "convergence_grant", "settled_fan_out"]
)
def test_the_compiling_observation_seats_the_bands_its_replayed_pass_seated(
    name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The compile and its ungranted replay read the reserved bands the last
    routing pass read: none where no settlement re-route consumed a ledger,
    and that re-route's own where one did.
    """
    if name == "convergence_grant":
        text, source_dir = UNSETTLED_CONVERGENCE_GRANT, ROOT
    else:
        path = (
            ROOT / "examples" / "guide" / "03_fan_out.mmd"
            if name == "settled_fan_out"
            else UNSETTLED_MAPS[name]
        )
        text, source_dir = path.read_text(), path.parent
    observed, _ = _observe_last_settle(monkeypatch, text, source_dir)

    compiling = _compiling_index(observed)
    assert not observed[compiling].plan.boundary_clearance_requirements
    replayed = observed[compiling - 1]
    assert replayed.bands is not None
    if name == "convergence_grant":
        assert len(observed) == compiling + 2
    for item in observed[compiling:]:
        assert item.bands == replayed.bands
