"""Content-placement phases are idempotent (the declarative property, #488).

Post-#465 the anchor layer is structural and frozen; the content-placement
phases position content as a function of those frozen anchors plus section
structure.  This test locks *idempotence*: applying any one phase a second
time, back-to-back on its own output, is a no-op (``P(P(x)) == P(x)``).

Idempotence is a fixed-point property and is strictly weaker than *purity*
(output a function of frozen anchors + structure only); the stronger property
is checked by ``test_content_placement_pure`` (#491).

Mechanism: monkeypatch every placement phase with a probe that applies it once,
snapshots the result, applies it a second time on that result, records any
station the second application moves, then restores the single-application
snapshot so the rest of the pipeline runs unperturbed.  Because each probe is
self-contained, all eight share one layout pass and a failure names the
offending phase.  A phase that reads its own prior output (e.g. selecting or
sorting by current Y) moves content on the second call and fails here.

Covers the whole render corpus so any fixture that exercises a phase
participates.  Refs #488, #465.
"""

from __future__ import annotations

import pytest
from conftest import (
    CONTENT_PLACEMENT_PHASES,
    Diff,
    compute_corpus_layout,
    content_corpus,
    diff_station_coords,
    restore_graph_state,
    snapshot_graph_state,
)

import nf_metro.layout.engine as engine
from nf_metro.parser.model import MetroGraph

CORPUS = content_corpus()


def _make_idempotence_probe(original, diffs: list[Diff]):
    """Wrap ``original`` to check ``P(P(x)) == P(x)`` locally, recording into
    ``diffs`` any station added, removed or moved by the second application."""

    def probe(graph: MetroGraph, *args, **kwargs):
        original(graph, *args, **kwargs)
        snap1 = snapshot_graph_state(graph)
        after1 = snap1[0]

        original(graph, *args, **kwargs)
        after2 = snapshot_graph_state(graph)[0]

        diffs.extend(diff_station_coords(after1, after2))

        restore_graph_state(graph, snap1)

    return probe


@pytest.mark.parametrize(
    "fid,path,is_nextflow", CORPUS, ids=[fid for fid, _, _ in CORPUS]
)
def test_placement_phase_is_idempotent(fid, path, is_nextflow, monkeypatch):
    """Every content-placement phase is idempotent on ``fid``.

    The probe checks ``P(P(x)) == P(x)`` directly at each phase and restores the
    single-application result, so all eight probes share one layout pass and a
    failure names the offending phase.  This is the literal property the file
    asserts, checked in isolation at the phase rather than inferred from the
    cascaded final layout, and covers the same (fixture, phase) pairs at
    one-eighth the layout cost.
    """
    diffs_by_phase: dict[str, list[Diff]] = {}
    for phase_name in CONTENT_PLACEMENT_PHASES:
        diffs_by_phase[phase_name] = []
        original = getattr(engine, phase_name)
        monkeypatch.setattr(
            engine,
            phase_name,
            _make_idempotence_probe(original, diffs_by_phase[phase_name]),
        )

    compute_corpus_layout(path, is_nextflow)

    non_idempotent = {name: diffs for name, diffs in diffs_by_phase.items() if diffs}
    assert not non_idempotent, (
        f"content-placement phase(s) not idempotent on {fid}: applying twice "
        f"moved content. Stations per phase (station: first -> second): "
        + "; ".join(
            f"{name}: {[(s, a, b) for s, a, b in diffs[:8]]}"
            for name, diffs in non_idempotent.items()
        )
    )
