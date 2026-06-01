"""Content-placement phases are idempotent (the declarative property, #488).

Post-#465 the anchor layer is structural and frozen; the content-placement
phases position content as a function of those frozen anchors plus section
structure.  This test locks the stronger property that each phase is also a
*pure function of its input state*: applying it a second time, back-to-back on
its own output, is a no-op.

Mechanism: monkeypatch the engine's reference to one placement phase with a
wrapper that invokes it twice, run the full layout (with the anchor guard
active), and assert every station coordinate matches the single-application
baseline.  A phase that reads its own prior output (e.g. selecting/sorting by
current Y) diverges on the second call and fails here.

Covers the whole render corpus so any fixture that exercises a phase
participates.  Refs #488, #465.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import nf_metro.layout.engine as engine
from nf_metro.convert import convert_nextflow_dag
from nf_metro.layout.engine import compute_layout
from nf_metro.parser.mermaid import parse_metro_mermaid

ROOT = Path(__file__).parent.parent
EXAMPLES = ROOT / "examples"
TOPOLOGIES = EXAMPLES / "topologies"
GUIDE = EXAMPLES / "guide"
TESTS_FIXTURES = ROOT / "tests" / "fixtures"
NEXTFLOW = TESTS_FIXTURES / "nextflow"

# The content-placement phases wrapped by _run_placement / _run_placement_per_row
# in _compute_section_layout (the set guarded by _guard_anchors_frozen).
PLACEMENT_PHASES = [
    "_redistribute_fanout_siblings",  # Stage 4.9
    "_redistribute_full_bundle_columns",  # Stage 4.10
    "_fan_free_content_upward",  # Stage 6.1
    "_fan_source_inputs_upward",  # Stage 6.2
    "_apply_half_grid_2branch_symfan",  # Stage 6.3
    "_recenter_full_bundle_columns",  # Stage 6.7
    "_balance_section_content_around_trunk",  # Stage 6.11
    "_recenter_loop_side_stations",  # Stage 6.12
]


def _corpus() -> list[tuple[str, Path, bool]]:
    items: list[tuple[str, Path, bool]] = []
    for d, tag in [
        (EXAMPLES, "examples"),
        (TOPOLOGIES, "topologies"),
        (GUIDE, "guide"),
        (TESTS_FIXTURES, "tests"),
    ]:
        for p in sorted(d.glob("*.mmd")):
            items.append((f"{tag}/{p.stem}", p, False))
    for p in sorted(NEXTFLOW.glob("*.mmd")):
        items.append((f"nextflow/{p.stem}", p, True))
    return items


CORPUS = _corpus()


def _layout(path: Path, is_nextflow: bool):
    text = path.read_text()
    if is_nextflow:
        text = convert_nextflow_dag(text)
    graph = parse_metro_mermaid(text)
    compute_layout(graph, validate=True)
    return graph


def _coords(graph) -> dict[str, tuple[float, float]]:
    return {sid: (round(s.x, 6), round(s.y, 6)) for sid, s in graph.stations.items()}


@pytest.mark.parametrize("phase_name", PLACEMENT_PHASES)
@pytest.mark.parametrize("fixture", CORPUS, ids=[fid for fid, _, _ in CORPUS])
def test_placement_phase_is_idempotent(fixture, phase_name, monkeypatch):
    fid, path, is_nextflow = fixture

    baseline = _coords(_layout(path, is_nextflow))

    original = getattr(engine, phase_name)

    def run_twice(graph, *args, **kwargs):
        original(graph, *args, **kwargs)
        original(graph, *args, **kwargs)

    monkeypatch.setattr(engine, phase_name, run_twice)
    doubled = _coords(_layout(path, is_nextflow))

    diff = {
        k: (baseline[k], doubled.get(k))
        for k in baseline
        if baseline[k] != doubled.get(k)
    }
    assert doubled == baseline, (
        f"{phase_name} is not idempotent on {fid}: applying it twice moved "
        f"content. Differing stations: {diff}"
    )
