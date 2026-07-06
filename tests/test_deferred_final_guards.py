"""The validate=True final-guard checkpoint must see the geometry the renderer
draws, not a transient pre-bypass layout state (#1339).

``compute_layout`` lays the graph out, then ``apply_geometric_bypass`` may
re-lay it with bypass helpers, moving sections to their final positions.  The
closing ``after final`` guard checkpoint has to run *after* that re-lay, so a
``validate=True`` render asserts the same section geometry a ``validate=False``
render (and the SVG renderer) produces.  Running it on the pre-bypass state
lets a final-only geometry guard (``_guard_no_route_through_section`` and the
inter-section wrap family) abort on a crossing the settled layout routes clear
of -- a false abort that blocks a pipeline whose actual output is fine.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nf_metro.layout import engine
from nf_metro.parser.mermaid import parse_metro_mermaid

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"

# Fixtures whose geometric-bypass pass keeps helpers and re-lays the graph, so
# the pre-bypass and settled section geometries genuinely differ.  These are
# the cases that expose a pre-final guard checkpoint; ``fold_bypass_creep`` is
# the sharp one (its ``report`` section drops ~44px between the two states).
_BYPASS_RELAY_FIXTURES = [
    "topologies/fold_bypass_creep.mmd",
    "topologies/fold_bypass_creep_tight.mmd",
    "topologies/inrow_skip_breeze.mmd",
    "topologies/rowmate_tb_side_entry_top_align_grow.mmd",
    "topologies/tb_fork_lane_transpose.mmd",
]


def _section_geometry(graph) -> dict[str, tuple[float, float, float, float]]:
    return {
        s.id: (
            round(s.bbox_x, 1),
            round(s.bbox_y, 1),
            round(s.bbox_x + s.bbox_w, 1),
            round(s.bbox_y + s.bbox_h, 1),
        )
        for s in graph.sections.values()
    }


@pytest.mark.parametrize("fixture", _BYPASS_RELAY_FIXTURES)
def test_after_final_checkpoint_sees_settled_geometry(fixture, monkeypatch) -> None:
    src = (EXAMPLES / fixture).read_text()

    observed: list[dict[str, tuple[float, float, float, float]]] = []
    real_run = engine.run_validate_guards

    def spy(graph, phase, **kwargs):
        if phase == "after final":
            observed.append(_section_geometry(graph))
        return real_run(graph, phase, **kwargs)

    monkeypatch.setattr(engine, "run_validate_guards", spy)

    graph = parse_metro_mermaid(src)
    engine.compute_layout(graph, validate=True)
    settled = _section_geometry(graph)

    assert observed, f"{fixture}: no 'after final' guard checkpoint ran"
    for i, snapshot in enumerate(observed):
        drift = {
            sid: (snapshot[sid], settled[sid])
            for sid in snapshot
            if snapshot[sid] != settled[sid]
        }
        assert not drift, (
            f"{fixture}: 'after final' checkpoint #{i} validated a pre-final "
            f"layout state the renderer never draws: {drift}"
        )
