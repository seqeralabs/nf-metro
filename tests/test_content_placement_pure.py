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

The remaining known leaks are pinned as strict xfails below and burned down by
the #491 follow-up PRs (track sorts, structural slack, structural fallback
trunks).  ``strict=True`` means a phase that *becomes* pure flips its xfail to
an xpass and fails the suite, forcing the stale entry to be removed.

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

KNOWN_LEAKS: frozenset[tuple[str, str]] = frozenset(
    {
        ("_redistribute_fanout_siblings", "examples/differentialabundance"),
        ("_balance_section_content_around_trunk", "examples/differentialabundance"),
        ("_balance_section_content_around_trunk", "examples/genomic_pipeline"),
    }
)


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


def _cases():
    for fid, path, is_nf in CORPUS:
        for phase in CONTENT_PLACEMENT_PHASES:
            marks = (
                [pytest.mark.xfail(strict=True, reason="#491 known purity leak")]
                if (phase, fid) in KNOWN_LEAKS
                else []
            )
            yield pytest.param(
                fid, path, is_nf, phase, id=f"{fid}-{phase}", marks=marks
            )


@pytest.mark.parametrize("fid,path,is_nf,phase_name", list(_cases()))
def test_placement_phase_is_pure(fid, path, is_nf, phase_name, monkeypatch):
    leaks: list[tuple[str, float, float]] = []
    original = getattr(engine, phase_name)
    monkeypatch.setattr(engine, phase_name, _make_purity_probe(original, leaks))
    compute_corpus_layout(path, is_nf)

    assert not leaks, (
        f"{phase_name} is not pure on {fid}: perturbing non-anchor state "
        f"(current Y + section bbox) changed where it placed content. "
        f"Leaks (station: baseline_y -> perturbed_y): "
        f"{[(s, round(a, 1), round(b, 1)) for s, a, b in leaks[:8]]}"
    )
