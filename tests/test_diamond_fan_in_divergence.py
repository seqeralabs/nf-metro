"""Tests for the shared fan-in divergence of a fork/join diamond.

Every peeled branch of a symmetric fork/join diamond crosses the same column gap
to reach the join hub, so the hub's reservation sets where each one peels off and
they leave together: one fan, converging on one point.

An edge that both forks and joins seats its diagonal at whichever end carries the
wider fan, ties keeping the fork end.  A branch that also emits a second line is
such an edge, and if the fork end wins there its fan-in divergence is measured
from the branch rather than from the hub -- past the branch's own name label, so
the diagonal clears the text -- putting each row at a different place, scattered
by that row's label width.  Such a seat also lies inside the label's x-extent,
leaving the strike-clearance loop a strike to grow runway columns against.

Covers:

* Happy-path: every shipped example and topology fixture peels its symmetric
  diamond branches off together.
* Targeted: six branches with labels of very different widths, each also feeding
  a downstream section, diverge at one shared position and grow no
  strike-clearance runway.
* Meaningfulness: seating those legs at the fork instead scatters them by roughly
  the spread of the branch labels' half-widths, and trips the invariant.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import nf_metro.layout.routing.intra_handlers as intra_handlers
from nf_metro.layout.engine import compute_layout
from nf_metro.layout.routing import compute_station_offsets, route_edges
from nf_metro.layout.routing.invariants import check_diamond_fan_in_diverges_together
from nf_metro.parser.mermaid import parse_metro_mermaid

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = REPO_ROOT / "examples"
FIXTURES = REPO_ROOT / "tests" / "fixtures"

BRANCHES = (
    "oma_online",
    "oma_local",
    "panther_online",
    "panther_local",
    "inspector_online",
    "eggnog_local",
)

# A reduction of nf-core/reportho's "Fetch orthologs": six branches of one
# symmetric diamond between two blank-label hubs, each also feeding a downstream
# section on a second line.  Held inline rather than in ``tests/fixtures`` because
# it draws a label strike the engine cannot clear, so a committed fixture would
# abort every corpus sweep that lays out under ``validate=True``.
SECOND_OUTPUT_DIAMOND = """%%metro title: Symmetric fan branch with a second output
%%metro diamond_style: symmetric
%%metro line: main | Main flow | #24b064
%%metro line: report | Report flow | #0dcaf0

graph LR
    subgraph input [Input]
        id_tax [Identify taxon]
    end

    subgraph fetch_ortho [Fetch orthologs]
        _ortho_entry [ ]
        oma_online [OMA online]
        oma_local [OMA local]
        panther_online [PANTHER online]
        panther_local [PANTHER local]
        inspector_online [OrthoInspector online]
        eggnog_local [EggNOG local]
        _ortho_exit [ ]

        _ortho_entry -->|main| oma_online
        _ortho_entry -->|main| oma_local
        _ortho_entry -->|main| panther_online
        _ortho_entry -->|main| panther_local
        _ortho_entry -->|main| inspector_online
        _ortho_entry -->|main| eggnog_local
        oma_online -->|main| _ortho_exit
        oma_local -->|main| _ortho_exit
        panther_online -->|main| _ortho_exit
        panther_local -->|main| _ortho_exit
        inspector_online -->|main| _ortho_exit
        eggnog_local -->|main| _ortho_exit
    end

    subgraph report [Report]
        generate_reports [Generate reports]
    end

    id_tax -->|main| _ortho_entry
    _ortho_exit -->|main| generate_reports

    oma_online -->|report| generate_reports
    oma_local -->|report| generate_reports
    panther_online -->|report| generate_reports
    panther_local -->|report| generate_reports
    inspector_online -->|report| generate_reports
    eggnog_local -->|report| generate_reports
"""


def _gather_fixtures() -> list[Path]:
    paths: list[Path] = []
    paths.extend(sorted(EXAMPLES.glob("*.mmd")))
    paths.extend(sorted((EXAMPLES / "topologies").glob("*.mmd")))
    paths.extend(sorted((EXAMPLES / "guide").glob("*.mmd")))
    paths.extend(sorted(FIXTURES.glob("*.mmd")))
    return paths


def _route(text: str, source_dir: str = ""):
    graph = parse_metro_mermaid(text)
    graph.source_dir = source_dir
    compute_layout(graph)
    offsets = compute_station_offsets(graph)
    return graph, route_edges(graph, station_offsets=offsets), offsets


@pytest.mark.parametrize(
    "path", _gather_fixtures(), ids=lambda p: p.relative_to(REPO_ROOT).as_posix()
)
def test_diamond_branches_diverge_together_in_gallery(path: Path) -> None:
    graph, routes, offsets = _route(path.read_text(), str(path.parent))
    violations = check_diamond_fan_in_diverges_together(graph, routes, offsets)
    assert not violations, "\n".join(v.message() for v in violations)


def test_second_output_branches_diverge_at_one_position() -> None:
    """The six branches peel off for the join hub together, despite carrying
    labels from ``OMA local`` to ``OrthoInspector online``."""
    graph, routes, offsets = _route(SECOND_OUTPUT_DIAMOND)
    assert not check_diamond_fan_in_diverges_together(graph, routes, offsets)


def test_second_output_diamond_grows_no_strike_runway() -> None:
    """A divergence seated off the join hub falls clear of every branch's name
    label, so the clearance loop finds no strike to reserve columns against."""
    graph, _, _ = _route(SECOND_OUTPUT_DIAMOND)
    section = graph.sections["fetch_ortho"]
    assert section.label_strike_entry_cols == 0
    assert section.label_strike_exit_cols == 0
    assert section.label_strike_layer_gaps == {}


def test_fork_seating_scatters_the_fan_in(monkeypatch: pytest.MonkeyPatch) -> None:
    """Seating a fork-and-join edge at its fork end regardless of fan degree
    scatters the six divergences by roughly the spread of the branch labels'
    half-widths, so the invariant is not vacuous."""
    monkeypatch.setattr(intra_handlers, "prefers_join_bias", lambda ctx, edge: False)
    graph, routes, offsets = _route(SECOND_OUTPUT_DIAMOND)
    violations = check_diamond_fan_in_diverges_together(graph, routes, offsets)
    assert violations, "expected a scattered fan-in under fork seating"
    assert [v.join_id for v in violations] == ["_ortho_exit"]
    assert violations[0].spread > 20.0, violations[0].message()
    assert {sid for sid, _ in violations[0].divergences} <= set(BRANCHES)
