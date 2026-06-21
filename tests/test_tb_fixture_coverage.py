"""Fixture-per-branch coverage locks for TB routing special-cases.

Each minimal fixture in :data:`TB_BRANCH_FIXTURES` is the sole minimal exemplar
of one TB routing branch. The test asserts the fixture reaches that branch, so
an edit that stops exercising it fails loudly instead of silently dropping the
branch from the corpus.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import nf_metro.layout.routing.tb_handlers as tb
from nf_metro.layout.engine import compute_layout
from nf_metro.layout.routing import compute_station_offsets, route_edges
from nf_metro.parser.mermaid import parse_metro_mermaid
from nf_metro.parser.model import PortSide

TOPOLOGIES_DIR = Path(__file__).parent.parent / "examples" / "topologies"


def _routing_cases(path: Path) -> set[str]:
    """Route a fixture and return the set of TB handler cases it reaches.

    The TB shape dispatch freezes the handler references in a module-level
    tuple at import; we wrap the handlers and rebuild the tuple so each claimed
    edge records a descriptive case key (handler + the relevant port side).
    """
    cases: set[str] = set()

    def side(ctx, pid: str) -> str:
        port = ctx.graph.ports.get(pid)
        return port.side.name if port else "??"

    originals = {}

    def patch(name: str, keyfn):
        orig = getattr(tb, name)
        originals[name] = orig

        def wrapped(edge, src, tgt, ctx, *a, **k):
            res = orig(edge, src, tgt, ctx, *a, **k)
            if res is not None:
                cases.add(keyfn(edge, ctx))
            return res

        setattr(tb, name, wrapped)

    patch("_route_tb_lr_exit", lambda e, c: f"lr_exit:{side(c, e.target)}")
    patch("_route_tb_lr_entry", lambda e, c: f"lr_entry:{side(c, e.source)}")
    patch("_route_perp_entry", lambda e, c: f"perp_entry:{side(c, e.source)}")
    patch("_route_tb_internal", lambda e, c: "internal")
    patch("_route_perp_entry_l_shape", lambda e, c: "perp_entry:l_shape")
    patch("_route_perp_entry_staircase", lambda e, c: "perp_entry:staircase")

    # _route_tb_internal claims diagonals via _route_tb_diagonal; record that
    # leg distinctly so the diagonal branch is pinned, not just the straight one.
    diag_orig = tb._route_tb_diagonal
    originals["_route_tb_diagonal"] = diag_orig

    def diag_wrapped(*a, **k):
        cases.add("internal:diagonal")
        return diag_orig(*a, **k)

    tb._route_tb_diagonal = diag_wrapped

    saved_tuple = tb._TB_SECTION_SHAPES
    tb._TB_SECTION_SHAPES = (
        tb._route_tb_internal,
        tb._route_tb_lr_exit,
        tb._route_tb_lr_entry,
        tb._route_perp_entry,
    )
    try:
        graph = parse_metro_mermaid(path.read_text(), max_station_columns=15)
        compute_layout(graph, validate=False)
        offsets = compute_station_offsets(graph)
        route_edges(graph, station_offsets=offsets)
    finally:
        for name, orig in originals.items():
            setattr(tb, name, orig)
        tb._TB_SECTION_SHAPES = saved_tuple
    return cases


# fixture stem -> the TB branch case it is the minimal exemplar for.
TB_BRANCH_FIXTURES = {
    "tb_lr_exit_left": "lr_exit:LEFT",
    "tb_lr_exit_right": "lr_exit:RIGHT",
    "tb_internal_diagonal": "internal:diagonal",
}


@pytest.mark.parametrize("stem,case", sorted(TB_BRANCH_FIXTURES.items()))
def test_tb_fixture_exercises_its_branch(stem: str, case: str) -> None:
    path = TOPOLOGIES_DIR / f"{stem}.mmd"
    assert path.exists(), f"missing fixture {path}"
    cases = _routing_cases(path)
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


def test_exit_port_side_constant_matches() -> None:
    """Guard against a PortSide rename desyncing this lock from the model."""
    assert {PortSide.LEFT.name, PortSide.RIGHT.name} == {"LEFT", "RIGHT"}
