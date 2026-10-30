"""The aperture re-route starts from the station placement the drawn pass read.

Routing centres a bypass helper between its diagonal and the flat run beyond
it, moving it half the remaining distance each time it is routed from its own
output.  The aperture re-route is one routing pass more than the drawn pass, so
seeded from the drawn pass's output it carries such a helper a further half
step, onto a twin diagonal of the same edge.  Seeded from the drawn pass's
input, translated by the aperture grant, it re-derives the drawn centring.
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass, field

import pytest

import nf_metro.render.svg as svg
from nf_metro.api import render_string
from nf_metro.layout.envelope_settlement import EnvelopeSettlement
from nf_metro.layout.settlement_demand import SettlementAxis
from nf_metro.parser.model import MetroGraph

# A row-1 corridor-cohort aperture is owed, and the drawn pass centres the
# bypass helper on the q1x1 -> q2x0 edge.
APERTURE_AFTER_CENTRING = """\
%%metro line: m0 | L0 | #e6194b
%%metro line: m1 | L1 | #3cb44b
%%metro line: m2 | L2 | #4363d8
%%metro line: m3 | L3 | #f58231
%%metro line: m4 | L4 | #911eb4
graph LR
    subgraph z0 [Zone 0]
        %%metro direction: BT
        q0x0 -->|m2,m4| q0x1
        q0x1 -->|m1,m3| q0x2
    end
    subgraph z1 [Zone 1]
        q1x0 -->|m3,m4| q1x1
        q1x0 -->|m2| q1x2
        q1x1 -->|m3| q1x2
        q1x2 -->|m2| q1x3
    end
    subgraph z2 [Zone 2]
        q2x0 -->|m1,m2,m3| q2x1
    end
    subgraph z3 [Zone 3]
        q3x0[Q3X0]
        q3x3[Q3X3]
    end
    subgraph z4 [Zone 4]
        %%metro direction: BT
        q4x2 -->|m2| q4x3
    end
    q0x0 -->|m0| q3x0
    q0x1 -->|m4| q2x0
    q0x2 -->|m3| q1x0
    q0x2 -->|m1| q2x0
    q1x1 -->|m0,m4| q2x0
    q1x2 -->|m3| q2x0
    q2x0 -->|m0| q3x0
    q2x1 -->|m1| q3x3
    q2x1 -->|m2| q4x2
"""


@dataclass
class _Observation:
    x_in: dict[str, float]
    x_out: dict[str, float]
    section_of: dict[str, str]


@dataclass
class _Trace:
    observations: list[_Observation] = field(default_factory=list)
    aperture_grants: list[tuple[int, EnvelopeSettlement]] = field(default_factory=list)


def _non_port_x(graph: MetroGraph) -> dict[str, float]:
    return {sid: st.x for sid, st in graph.stations.items() if not st.is_port}


def _trace_render(text: str, monkeypatch: pytest.MonkeyPatch) -> tuple[_Trace, str]:
    trace = _Trace()
    real_observe = svg.observe_route_edges_centred
    real_grant_closes = svg._assert_aperture_grant_closes

    def observe(graph, **kwargs):
        x_in = _non_port_x(graph)
        section_of = {
            sid: section_id
            for section_id, section in graph.sections.items()
            for sid in section.station_ids
        }
        observation = real_observe(graph, **kwargs)
        trace.observations.append(_Observation(x_in, _non_port_x(graph), section_of))
        return observation

    def grant_closes(owed, settlement):
        trace.aperture_grants.append((len(trace.observations), settlement))
        return real_grant_closes(owed, settlement)

    monkeypatch.setattr(svg, "observe_route_edges_centred", observe)
    monkeypatch.setattr(svg, "_assert_aperture_grant_closes", grant_closes)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        rendered = render_string(text)
    return trace, rendered


def test_aperture_reroute_reads_the_drawn_inputs_translated_by_the_grant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trace, rendered = _trace_render(APERTURE_AFTER_CENTRING, monkeypatch)

    assert rendered.lstrip().startswith("<")
    assert len(trace.aperture_grants) == 1, (
        "the map does not take the aperture re-route, so this test witnesses "
        "nothing; find another map that owes a corridor-cohort aperture after "
        "a drawn pass that centres a station"
    )
    granted_after, settlement = trace.aperture_grants[0]
    # The aperture observation replays the drawn pass's inputs, and the re-route
    # follows the grant as the last routing pass.
    assert len(trace.observations) == granted_after + 1
    drawn, replay, reroute = trace.observations[granted_after - 2 :]
    assert replay.x_in == drawn.x_in
    assert any(abs(drawn.x_out[sid] - x) > 0.01 for sid, x in drawn.x_in.items()), (
        "the drawn pass centred no station, so the start of the re-route is moot"
    )

    column_shift: dict[str, float] = {}
    for translation in settlement.translations:
        if translation.axis is SettlementAxis.COLUMN:
            for section_id in translation.section_ids:
                column_shift[section_id] = (
                    column_shift.get(section_id, 0.0) + translation.amount
                )
    expected = {
        sid: x + column_shift.get(drawn.section_of.get(sid, ""), 0.0)
        for sid, x in drawn.x_in.items()
    }
    assert reroute.x_in == pytest.approx(expected, abs=0.01)
