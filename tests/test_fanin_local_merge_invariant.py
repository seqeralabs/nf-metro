"""Tests for the local-merge invariant on fan-in to a distant terminus.

When 2+ sibling stations sharing a layer feed one terminus (a file/dir/report
icon), the convergence junction inserted before the terminus must sit close to
the siblings, so they meet promptly.  If a longer parallel path pushes the
terminus far to the right, the short-path siblings would otherwise run parallel
all the way to it, bowing the fan out to fill the whole gap (issue #1296).

Covers:

* Happy-path: every gallery example and topology fixture keeps same-layer
  fan-in siblings merging within the tolerated span (no distant-terminus bow).
* The reported fixture ``fanin_distant_terminus`` -- two methods feed a report
  pushed far right by a third method's parallel ORF chain -- merges locally.
* Meaningfulness: routing every source through one terminus junction (the
  pre-fix behaviour) makes the checker fire on the reported fixture.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nf_metro.layout.engine import compute_layout
from nf_metro.layout.phases.guards import _converge_sibling_merge_violations
from nf_metro.parser.mermaid import parse_metro_mermaid
from nf_metro.parser.model import Edge

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = REPO_ROOT / "examples"
EXAMPLE_TOPOLOGIES = EXAMPLES / "topologies"
FIXTURE_TOPOLOGIES = REPO_ROOT / "tests" / "fixtures" / "topologies"


def _gather_fixtures() -> list[Path]:
    paths: list[Path] = []
    paths.extend(sorted(EXAMPLES.glob("*.mmd")))
    paths.extend(sorted(EXAMPLE_TOPOLOGIES.glob("*.mmd")))
    paths.extend(sorted(FIXTURE_TOPOLOGIES.glob("*.mmd")))
    return paths


def _layout(path: Path):
    graph = parse_metro_mermaid(path.read_text())
    compute_layout(graph, validate=False)
    return graph


@pytest.mark.parametrize(
    "path", _gather_fixtures(), ids=lambda p: p.relative_to(REPO_ROOT).as_posix()
)
def test_fanin_siblings_merge_locally_in_gallery(path: Path) -> None:
    """No shipped fixture leaves same-layer fan-in siblings running parallel to
    a distant terminus."""
    graph = _layout(path)
    violations = list(_converge_sibling_merge_violations(graph))
    assert not violations, f"{path.name}: late fan-in merges " + ", ".join(
        f"junction {jid} (layer {jl}) merges {n} siblings at layer {sl}"
        for jid, jl, sl, n in violations
    )


def test_distant_terminus_fan_merges_locally() -> None:
    """The reported fixture's two direct method siblings merge one layer
    downstream, not at the report pushed far right by the parallel ORF chain."""
    graph = _layout(EXAMPLE_TOPOLOGIES / "fanin_distant_terminus.mmd")
    siblings = [graph.stations[s] for s in ("anota", "delta")]
    sib_layer = siblings[0].layer
    assert all(s.layer == sib_layer for s in siblings)
    # Each sibling's sole successor (the merge junction) sits one layer on.
    for sib in siblings:
        succ_layers = {
            graph.stations[e.target].layer for e in graph.edges if e.source == sib.id
        }
        assert succ_layers == {sib_layer + 1}, (
            f"{sib.id} merges at layers {succ_layers}, expected {sib_layer + 1}"
        )
    assert not list(_converge_sibling_merge_violations(graph))


def test_checker_fires_on_single_distant_junction() -> None:
    """A single terminus junction merging all sources at the far column (the
    pre-fix shape) trips the checker, so the invariant encodes the bug.

    The siblings sit three columns upstream of the junction (past the
    ``_MAX_SIBLING_MERGE_SLACK`` tolerance), the distant-terminus bow the fix
    exists to prevent."""
    text = """%%metro title: Single distant junction
%%metro file: report | HTML | Report
%%metro line: a | Line A | #e6007e

graph LR
    subgraph s1 [S1]
        prep[Prep]
        m1[M1]
        m2[M2]
        m3[M3]
        j[Merge]
        p1[P1]
        p2[P2]
        p3[P3]
        prep -->|a| m1
        prep -->|a| m2
        prep -->|a| m3
        prep -->|a| p1
        p1 -->|a| p2
        p2 -->|a| p3
        m1 -->|a| j
        m2 -->|a| j
        m3 -->|a| j
        p3 -->|a| j
        j -->|a| report
    end
"""
    graph = parse_metro_mermaid(text)
    compute_layout(graph, validate=False)
    # Rename the explicit merge station to a convergence-junction id so the
    # checker inspects it the way it inspects the synthesized junction.
    j = graph.stations.pop("j")
    j.id = "__converge_report_9"
    graph.stations["__converge_report_9"] = j
    graph.replace_edges(
        [
            Edge(
                source="__converge_report_9" if e.source == "j" else e.source,
                target="__converge_report_9" if e.target == "j" else e.target,
                line_id=e.line_id,
            )
            for e in graph.edges
        ]
    )
    assert list(_converge_sibling_merge_violations(graph))
