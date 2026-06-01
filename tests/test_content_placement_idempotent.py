"""Content-placement phases are idempotent (the declarative property, #488).

Post-#465 the anchor layer is structural and frozen; the content-placement
phases position content as a function of those frozen anchors plus section
structure.  This test locks *idempotence*: applying any one phase a second
time, back-to-back on its own output, is a no-op (``P(P(x)) == P(x)``).

Idempotence is a fixed-point property and is strictly weaker than *purity*
(output a function of frozen anchors + structure only); the stronger property
is checked by ``test_content_placement_pure`` (#491).

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
from conftest import CONTENT_PLACEMENT_PHASES, compute_corpus_layout, content_corpus

import nf_metro.layout.engine as engine
from nf_metro.parser.model import MetroGraph

CORPUS = content_corpus()


def _coords(graph: MetroGraph) -> dict[str, tuple[float, float]]:
    return {sid: (round(s.x, 6), round(s.y, 6)) for sid, s in graph.stations.items()}


# The single-pass baseline depends only on the fixture, so compute it once per
# fixture rather than re-running it for each of the eight phases.
_BASELINE: dict[str, dict[str, tuple[float, float]]] = {}


def _baseline(
    fid: str, path: Path, is_nextflow: bool
) -> dict[str, tuple[float, float]]:
    if fid not in _BASELINE:
        _BASELINE[fid] = _coords(compute_corpus_layout(path, is_nextflow))
    return _BASELINE[fid]


@pytest.mark.parametrize("phase_name", CONTENT_PLACEMENT_PHASES)
@pytest.mark.parametrize("fixture", CORPUS, ids=[fid for fid, _, _ in CORPUS])
def test_placement_phase_is_idempotent(fixture, phase_name, monkeypatch):
    fid, path, is_nextflow = fixture

    baseline = _baseline(fid, path, is_nextflow)

    original = getattr(engine, phase_name)

    def run_twice(graph, *args, **kwargs):
        original(graph, *args, **kwargs)
        original(graph, *args, **kwargs)

    monkeypatch.setattr(engine, phase_name, run_twice)
    doubled = _coords(compute_corpus_layout(path, is_nextflow))

    diff = {
        k: (baseline[k], doubled.get(k))
        for k in baseline
        if baseline[k] != doubled.get(k)
    }
    assert doubled == baseline, (
        f"{phase_name} is not idempotent on {fid}: applying it twice moved "
        f"content. Differing stations: {diff}"
    )
