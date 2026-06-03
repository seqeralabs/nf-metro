"""The ``validate`` flag of ``compute_layout`` is observational (#518).

``compute_layout(graph, validate=True)`` must produce *identical* final
station geometry to ``compute_layout(graph, validate=False)``.  The flag is
supposed to add invariant checks only; it must never perturb the layout.

A regression here was caused by ``_run_pass_c_guards`` calling
``route_edges``, whose diagonal-centring pass mutates ``Station.x`` in
place.  Run mid-pipeline under ``validate=True``, that mutation changed the
input to later stages, splitting same-column stations (e.g. ``gatk`` and
``deepvariant`` in ``variant_calling.mmd``).  The fix makes the guard
snapshot/restore station state so it stays non-mutating.

The render path (``render_svg``) runs with ``validate=False``, so the
perturbed geometry never shipped, but it blocked adding tight runtime
column/alignment guards (they fired on the ``validate=True`` artifact).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import content_corpus

from nf_metro.convert import convert_nextflow_dag
from nf_metro.layout.engine import compute_layout
from nf_metro.parser.mermaid import parse_metro_mermaid
from nf_metro.parser.model import MetroGraph

CORPUS = content_corpus()


def _layout(path: Path, is_nextflow: bool, *, validate: bool) -> MetroGraph:
    text = path.read_text()
    if is_nextflow:
        text = convert_nextflow_dag(text)
    graph = parse_metro_mermaid(text)
    compute_layout(graph, validate=validate)
    return graph


def _coords(graph: MetroGraph) -> dict[str, tuple[float, float]]:
    return {sid: (round(s.x, 6), round(s.y, 6)) for sid, s in graph.stations.items()}


@pytest.mark.parametrize("fixture", CORPUS, ids=[fid for fid, _, _ in CORPUS])
def test_validate_flag_does_not_change_geometry(fixture):
    fid, path, is_nextflow = fixture

    unvalidated = _coords(_layout(path, is_nextflow, validate=False))
    validated = _coords(_layout(path, is_nextflow, validate=True))

    diffs = {
        sid: (unvalidated[sid], validated[sid])
        for sid in unvalidated
        if unvalidated[sid] != validated.get(sid)
    }
    assert not diffs, (
        f"{fid}: validate=True perturbed station geometry vs validate=False; "
        f"the flag must be observational. Differing stations "
        f"(validate=False -> validate=True): {diffs}"
    )


@pytest.mark.parametrize("name", ["variant_calling", "variant_calling_tuned"])
def test_shared_column_survives_validate(name):
    """``gatk``/``deepvariant`` share a column; ``validate=True`` must keep it.

    Pins the exact pair named in #518 so the regression has a focused guard
    independent of the corpus parametrisation above.
    """
    path = Path(__file__).resolve().parent.parent / "examples" / f"{name}.mmd"
    unvalidated = _layout(path, False, validate=False)
    validated = _layout(path, False, validate=True)

    for sid in ("gatk", "deepvariant"):
        assert validated.stations[sid].x == pytest.approx(
            unvalidated.stations[sid].x
        ), f"{name}: {sid}.x diverged under validate=True"
    assert validated.stations["gatk"].x == pytest.approx(
        validated.stations["deepvariant"].x
    ), f"{name}: gatk/deepvariant must share their column under validate=True"
