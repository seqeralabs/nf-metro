"""Fixture-per-branch coverage locks for TB routing special-cases.

Each minimal fixture in :data:`TB_BRANCH_FIXTURES` is the sole minimal exemplar
of one TB routing branch. The test asserts the fixture reaches that branch, so
an edit that stops exercising it fails loudly instead of silently dropping the
branch from the corpus.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import parse_and_layout

import nf_metro.layout.routing.tb_handlers as tb
from nf_metro.layout.routing import compute_station_offsets, route_edges

TOPOLOGIES_DIR = Path(__file__).parent.parent / "examples" / "topologies"


def _routing_cases(path: Path) -> set[str]:
    """Route *path* and return the set of TB routing branches it reaches.

    ``_route_tb_lr_exit`` is dispatched through the first-match tuple
    ``_TB_SECTION_SHAPES``, frozen at import, so the tuple is re-pointed at the
    wrapped handler; ``_route_tb_diagonal`` is reached by a direct module-global
    call and so is wrapped in place.
    """
    cases: set[str] = set()

    with pytest.MonkeyPatch.context() as mp:
        lr_exit_orig = tb._route_tb_lr_exit

        def lr_exit(edge, src, tgt, ctx):
            res = lr_exit_orig(edge, src, tgt, ctx)
            if res is not None:
                cases.add(f"lr_exit:{ctx.graph.ports[edge.target].side.name}")
            return res

        diag_orig = tb._route_tb_diagonal

        def diagonal(*args, **kwargs):
            cases.add("internal:diagonal")
            return diag_orig(*args, **kwargs)

        mp.setattr(tb, "_route_tb_lr_exit", lr_exit)
        mp.setattr(tb, "_route_tb_diagonal", diagonal)
        mp.setattr(
            tb,
            "_TB_SECTION_SHAPES",
            tuple(getattr(tb, shape.__name__) for shape in tb._TB_SECTION_SHAPES),
        )

        graph = parse_and_layout(path.read_text(), validate=False)
        route_edges(graph, station_offsets=compute_station_offsets(graph))
    return cases


# fixture stem -> the TB branch case it is the minimal exemplar for.
TB_BRANCH_FIXTURES = {
    "tb_lr_exit_left": "lr_exit:LEFT",
    "tb_lr_exit_right": "lr_exit:RIGHT",
    "tb_internal_diagonal": "internal:diagonal",
}


@pytest.mark.parametrize("stem,case", sorted(TB_BRANCH_FIXTURES.items()))
def test_tb_fixture_exercises_its_branch(stem: str, case: str) -> None:
    cases = _routing_cases(TOPOLOGIES_DIR / f"{stem}.mmd")
    assert case in cases, (
        f"{stem}.mmd no longer exercises TB branch {case!r}; "
        f"it reached {sorted(cases)} instead"
    )


def test_lr_exit_sides_are_distinct_fixtures() -> None:
    """The LEFT and RIGHT exit fixtures must isolate opposite port sides."""
    left = _routing_cases(TOPOLOGIES_DIR / "tb_lr_exit_left.mmd")
    right = _routing_cases(TOPOLOGIES_DIR / "tb_lr_exit_right.mmd")
    assert "lr_exit:LEFT" in left and "lr_exit:RIGHT" not in left
    assert "lr_exit:RIGHT" in right and "lr_exit:LEFT" not in right
