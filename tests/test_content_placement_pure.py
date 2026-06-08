"""Content-placement phases are pure functions of (anchors + structure) (#491).

Idempotence (``test_content_placement_idempotent``) only proves each phase
reaches a fixed point: ``P(P(x)) == P(x)``.  That is strictly weaker than the
property the anchor layer enjoys (#487): *purity*.  A phase is pure when the Y
it assigns to every station it governs is a function of the **frozen anchors**
(port positions, and the trunk Y derived from them) plus **structure** (tracks,
edges, columns) ONLY -- never of the mutable, intermediate non-anchor state
that earlier phases happen to have left behind (current station Y, section
``bbox`` geometry).

Probe: wrap a phase and run it twice from the same graph -- once on the real
input, once after *perturbing* the non-anchor state (deterministically scramble
every non-port station's Y and every section's bbox top/height, holding all
port anchors frozen).  A pure phase governs the same stations and lands each at
the same Y both times.  Any station the phase moves in either run whose final Y
differs between the two runs is a purity leak.

All eight content-placement phases are pure, so this guard admits no
exceptions: it is the machine-checked counterpart to #487's anchor-frozen guard,
and a regression that reintroduces a non-anchor read fails it.

Refs #491, #488, #487, #485, #465.
"""

from __future__ import annotations

import pytest
from conftest import CONTENT_PLACEMENT_PHASES, compute_corpus_layout, content_corpus

import nf_metro.layout.engine as engine
from nf_metro.parser.model import MetroGraph

TOL = 1e-3

_Coords = dict[str, tuple[float, float]]

CORPUS = content_corpus()


def _snapshot(graph: MetroGraph) -> tuple[_Coords, _Coords]:
    stations = {sid: (s.x, s.y) for sid, s in graph.stations.items()}
    bboxes = {sec.id: (sec.bbox_y, sec.bbox_h) for sec in graph.sections.values()}
    return stations, bboxes


def _restore(graph: MetroGraph, snap: tuple[_Coords, _Coords]) -> None:
    stations, bboxes = snap
    for sid, (x, y) in stations.items():
        st = graph.stations.get(sid)
        if st is not None:
            st.x, st.y = x, y
    for sid, (y, h) in bboxes.items():
        sec = graph.sections.get(sid)
        if sec is not None:
            sec.bbox_y, sec.bbox_h = y, h


def _perturb(graph: MetroGraph) -> None:
    """Deterministically scramble the non-anchor state.

    Every non-port station's Y and every section's bbox top/height is shifted by
    a station/section-indexed amount; port stations (the frozen inter-section
    anchors) are left untouched, so the perturbation only stresses the mutable
    state a pure phase must not depend on.
    """
    ports = set(graph.ports)
    for i, (sid, st) in enumerate(sorted(graph.stations.items())):
        if sid in ports:
            continue
        st.y += ((i % 7) - 3) * 13.0
    for j, sec in enumerate(sorted(graph.sections.values(), key=lambda s: s.id)):
        sec.bbox_y += ((j % 5) - 2) * 11.0
        sec.bbox_h += ((j % 3) + 1) * 9.0


def _make_purity_probe(original, leaks: list[tuple[str, float, float]]):
    """Wrap ``original`` so each call records any non-anchor-state dependence
    into ``leaks`` and then leaves the genuine single-application result."""

    def probe(graph: MetroGraph, *args, **kwargs):
        pre = _snapshot(graph)
        before_y = {sid: s.y for sid, s in graph.stations.items()}

        original(graph, *args, **kwargs)
        base_after = {sid: s.y for sid, s in graph.stations.items()}
        governed = {
            sid for sid in base_after if abs(base_after[sid] - before_y[sid]) > TOL
        }

        _restore(graph, pre)
        _perturb(graph)
        pert_before = {sid: s.y for sid, s in graph.stations.items()}
        original(graph, *args, **kwargs)
        pert_after = {sid: s.y for sid, s in graph.stations.items()}
        governed |= {
            sid for sid in pert_after if abs(pert_after[sid] - pert_before[sid]) > TOL
        }

        for sid in governed:
            if abs(base_after[sid] - pert_after[sid]) > TOL:
                leaks.append((sid, base_after[sid], pert_after[sid]))

        _restore(graph, pre)
        original(graph, *args, **kwargs)

    return probe


def test_content_placement_phases_complete():
    """Completeness guard (#503): the set of phases actually run through the
    ``_run_placement`` wrapper in ``_compute_section_layout`` must equal the
    guarded ``CONTENT_PLACEMENT_PHASES`` set.

    ``_run_placement`` is the single chokepoint every content-placement phase
    flows through, and it records each ``fn.__name__`` into
    ``engine._PLACEMENT_PHASES_RUN``.  Rendering the whole corpus (which
    includes ``center_ports`` fixtures, exercising the gated 6.3 / 6.7 phases)
    accumulates the ground-truth run set.  Asserting it equals the guarded set
    means:

    - A new content phase wired through ``_run_placement`` but left out of
      ``CONTENT_PLACEMENT_PHASES`` shows up as *run but unguarded* and fails
      here -- forcing the dev to register it, at which point the purity and
      anchor-frozen guards make it declarative.
    - A stale name in ``CONTENT_PLACEMENT_PHASES`` that no longer runs shows up
      as *guarded but never run* and fails too.
    """
    engine._PLACEMENT_PHASES_RUN.clear()
    for _, path, is_nf in CORPUS:
        compute_corpus_layout(path, is_nf)
    run = set(engine._PLACEMENT_PHASES_RUN)
    guarded = set(CONTENT_PLACEMENT_PHASES)

    run_not_guarded = run - guarded
    guarded_not_run = guarded - run
    assert not run_not_guarded, (
        "content-placement phase(s) run through _run_placement but missing from "
        f"CONTENT_PLACEMENT_PHASES (so unguarded by purity / anchor-frozen): "
        f"{sorted(run_not_guarded)}. Register them in tests/conftest.py."
    )
    assert not guarded_not_run, (
        "CONTENT_PLACEMENT_PHASES lists phase(s) never run via _run_placement on "
        f"the corpus (stale entry?): {sorted(guarded_not_run)}."
    )


@pytest.mark.parametrize("fid,path,is_nf", CORPUS, ids=[fid for fid, _, _ in CORPUS])
def test_placement_phase_is_pure(fid, path, is_nf, monkeypatch):
    """Every content-placement phase is pure on ``fid``.

    Each phase's purity probe is self-contained -- it snapshots, measures, and
    restores the genuine single-application result around its own phase call --
    so all eight coexist in one layout pass without perturbing the pipeline or
    each other.  This checks the same (fixture, phase) coverage as eight
    separate runs would, at one-eighth the layout cost.
    """
    leaks_by_phase: dict[str, list[tuple[str, float, float]]] = {}
    for phase_name in CONTENT_PLACEMENT_PHASES:
        leaks_by_phase[phase_name] = []
        original = getattr(engine, phase_name)
        monkeypatch.setattr(
            engine, phase_name, _make_purity_probe(original, leaks_by_phase[phase_name])
        )

    compute_corpus_layout(path, is_nf)

    impure = {name: leaks for name, leaks in leaks_by_phase.items() if leaks}
    assert not impure, (
        f"content-placement phase(s) not pure on {fid}: perturbing non-anchor "
        f"state (current Y + section bbox) changed where they placed content. "
        f"Leaks per phase (station: baseline_y -> perturbed_y): "
        + "; ".join(
            f"{name}: {[(s, round(a, 1), round(b, 1)) for s, a, b in leaks[:8]]}"
            for name, leaks in impure.items()
        )
    )
