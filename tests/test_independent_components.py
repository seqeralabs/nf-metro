"""Independent placement of disconnected section components.

When the section meta-graph splits into two or more weakly-connected
components, each is placed in its own local column grid and the components
are stacked vertically.  A wide disconnected component must NOT inflate the
columns an unrelated connected trunk sits in.  A single-component graph (or
any graph carrying an explicit ``%%metro grid:`` override) is unchanged.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

from nf_metro.layout.engine import compute_layout
from nf_metro.layout.section_placement import _weakly_connected_components
from nf_metro.parser.mermaid import parse_metro_mermaid

EXAMPLES_DIR = Path(__file__).resolve().parent.parent / "examples"

# Geometry that must match the connected-only layout is compared with sub-pixel
# slack to absorb rounding, not to tolerate real drift.
_EQ_EPS = 0.5


def _layout(text: str):
    g = parse_metro_mermaid(text)
    compute_layout(g)
    return g


def _right(section) -> float:
    return section.offset_x + section.bbox_x + section.bbox_w


def _left(section) -> float:
    return section.offset_x + section.bbox_x


# A connected 3-section trunk (A -> B -> C) plus a separate, very wide
# disconnected section D.  In the old shared-grid placement D's width
# inflated column 0 and flung B and C far to the right.
_TRUNK_PLUS_WIDE = """%%metro title: Trunk Plus Wide
%%metro line: main | Main | #e63946
%%metro line: aux | Aux | #0570b0

graph LR
    subgraph a [A]
        a1[A1]
        a2[A2]
        a1 -->|main| a2
    end
    subgraph b [B]
        b1[B1]
        b2[B2]
        b1 -->|main| b2
    end
    subgraph c [C]
        c1[C1]
        c2[C2]
        c1 -->|main| c2
    end
    subgraph d [D]
        d1[D1]
        d2[D2]
        d3[D3]
        d4[D4]
        d5[D5]
        d6[D6]
        d1 -->|aux| d2
        d2 -->|aux| d3
        d3 -->|aux| d4
        d4 -->|aux| d5
        d5 -->|aux| d6
    end
    a2 -->|main| b1
    b2 -->|main| c1
"""

# The same connected trunk WITHOUT the wide disconnected section.
_TRUNK_ONLY = """%%metro title: Trunk Only
%%metro line: main | Main | #e63946

graph LR
    subgraph a [A]
        a1[A1]
        a2[A2]
        a1 -->|main| a2
    end
    subgraph b [B]
        b1[B1]
        b2[B2]
        b1 -->|main| b2
    end
    subgraph c [C]
        c1[C1]
        c2[C2]
        c1 -->|main| c2
    end
    a2 -->|main| b1
    b2 -->|main| c1
"""


def test_wide_disconnected_section_does_not_push_trunk_right() -> None:
    """The wide component must not widen the trunk's columns.

    Each trunk section must start no further right than the previous
    trunk section's right edge plus the disconnected component's width --
    i.e. the trunk's own span, not the wide panel's span, bounds it.  We
    assert the strong form: every trunk section stays left of where it
    would land if the wide section's width leaked into the shared grid.
    """
    g = _layout(_TRUNK_PLUS_WIDE)

    a, b, c, d = (g.sections[s] for s in ("a", "b", "c", "d"))

    # The wide section D is genuinely much wider than any trunk section.
    trunk_max_w = max(s.bbox_w for s in (a, b, c))
    assert d.bbox_w > trunk_max_w * 2

    # The trunk packs against its own widths: each later section begins
    # within a couple of column gaps of its predecessor's right edge, far
    # less than the wide section's width.
    assert _left(b) - _right(a) < d.bbox_w
    assert _left(c) - _right(b) < d.bbox_w

    # The trunk's internal geometry does not depend on D's width: its
    # inter-section gaps and total span are identical whether or not the
    # wide panel is present.  (Only the global left/top canvas margin may
    # differ, since the panel changes the overall canvas extent.)
    g_no_d = _layout(_TRUNK_ONLY)
    na, nb, nc = (g_no_d.sections[s] for s in ("a", "b", "c"))

    span_with = _right(c) - _left(a)
    span_without = _right(nc) - _left(na)
    assert abs(span_with - span_without) < _EQ_EPS

    gap_ab_with = _left(b) - _right(a)
    gap_ab_without = _left(nb) - _right(na)
    assert abs(gap_ab_with - gap_ab_without) < _EQ_EPS

    gap_bc_with = _left(c) - _right(b)
    gap_bc_without = _left(nc) - _right(nb)
    assert abs(gap_bc_with - gap_bc_without) < _EQ_EPS


def test_disconnected_components_are_separated_vertically() -> None:
    """Stacked components do not overlap vertically."""
    g = _layout(_TRUNK_PLUS_WIDE)
    a = g.sections["a"]  # trunk top
    d = g.sections["d"]  # wide panel, stacked below
    trunk_bottom = max(
        s.offset_y + s.bbox_y + s.bbox_h
        for s in (g.sections["a"], g.sections["b"], g.sections["c"])
    )
    assert d.offset_y + d.bbox_y >= trunk_bottom - _EQ_EPS
    assert a.offset_y + a.bbox_y < d.offset_y + d.bbox_y


def test_top_component_keeps_single_grid_origin() -> None:
    """The top stacked component must not gain a gratuitous down/right shift.

    Disconnected placement anchors the whole stack to the first (top)
    component's natural top-left, so that component lands at exactly the
    same origin it would in a single connected layout.  Here the top
    component is the A->B->C trunk; laid out on its own it must sit at the
    same left/top as it does in the full disconnected graph.
    """
    g = _layout(_TRUNK_PLUS_WIDE)
    g_trunk = _layout(_TRUNK_ONLY)

    a, na = g.sections["a"], g_trunk.sections["a"]
    assert abs(_left(a) - _left(na)) < _EQ_EPS
    trunk_top = min(
        s.offset_y + s.bbox_y
        for s in (g.sections["a"], g.sections["b"], g.sections["c"])
    )
    trunk_only_top = min(
        s.offset_y + s.bbox_y
        for s in (g_trunk.sections["a"], g_trunk.sections["b"], g_trunk.sections["c"])
    )
    assert abs(trunk_top - trunk_only_top) < _EQ_EPS


def test_single_component_placement_unchanged() -> None:
    """A connected graph yields exactly one component and the legacy path."""
    text = (EXAMPLES_DIR / "topologies" / "section_diamond.mmd").read_text()
    g = parse_metro_mermaid(text)
    comps = _weakly_connected_components(g, g.section_dag.section_edges)
    assert len(comps) == 1
    assert comps[0] == set(g.sections)


def test_explicit_grid_keeps_shared_grid() -> None:
    """An explicit %%metro grid: override disables independent stacking.

    self_crossing_bridge deliberately interleaves two components in one
    shared grid via explicit grid hints; that must be honoured.
    """
    text = (EXAMPLES_DIR / "topologies" / "self_crossing_bridge.mmd").read_text()
    g = parse_metro_mermaid(text)
    assert g._explicit_grid  # author pinned positions
    comps = _weakly_connected_components(g, g.section_dag.section_edges)
    assert len(comps) == 2
    # Layout still succeeds and respects the explicit interleaving: the
    # mid component (row 1) sits between top (row 0) and bus_sink (row 2).
    compute_layout(g)
    top_y = g.sections["top"].offset_y + g.sections["top"].bbox_y
    mid_y = g.sections["mid_src"].offset_y + g.sections["mid_src"].bbox_y
    sink_y = g.sections["bus_sink"].offset_y + g.sections["bus_sink"].bbox_y
    assert top_y < mid_y < sink_y


# A connected trunk wide enough to FOLD (its station-column count exceeds the
# 15-column threshold, so auto-layout serpentines it across grid rows 0..2)
# coexisting with a separate, disconnected section.  This is the topology the
# component partition-and-stack path is most exposed to hash-order
# nondeterminism on: ``networkx.weakly_connected_components`` yields ``set``
# objects, and the set-built BFS adjacency that drives column grouping and row
# packing must be traversed in a stable order or a trunk section's ``grid_row``
# flips between runs (and the components stack in a different vertical order).
_FOLDING_TRUNK_PLUS_DISC = """%%metro title: Folding Trunk Plus Disc
%%metro line: main | Main | #e63946
%%metro line: aux | Aux | #0570b0

graph LR
    subgraph disc [Disc]
        da[a]
        db[b]
        dc[c]
        dd[d]
        de[e]
        df[f]
        da-->|aux|db
        db-->|aux|dc
        dc-->|aux|dd
        dd-->|aux|de
        de-->|aux|df
    end
    subgraph k1 [K1]
        k1a[a]
        k1b[b]
        k1c[c]
        k1d[d]
        k1e[e]
        k1a-->|main|k1b
        k1b-->|main|k1c
        k1c-->|main|k1d
        k1d-->|main|k1e
    end
    subgraph k2 [K2]
        k2a[a]
        k2b[b]
        k2c[c]
        k2d[d]
        k2e[e]
        k2a-->|main|k2b
        k2b-->|main|k2c
        k2c-->|main|k2d
        k2d-->|main|k2e
    end
    subgraph k3 [K3]
        k3a[a]
        k3b[b]
        k3c[c]
        k3d[d]
        k3e[e]
        k3a-->|main|k3b
        k3b-->|main|k3c
        k3c-->|main|k3d
        k3d-->|main|k3e
    end
    subgraph k4 [K4]
        k4a[a]
        k4b[b]
        k4c[c]
        k4d[d]
        k4e[e]
        k4a-->|main|k4b
        k4b-->|main|k4c
        k4c-->|main|k4d
        k4d-->|main|k4e
    end
    k1e -->|main| k2a
    k2e -->|main| k3a
    k3e -->|main| k4a
"""


def _placement_signature(g) -> tuple:
    """A stable signature of every section's grid + canvas placement."""
    return tuple(
        (
            sid,
            g.sections[sid].grid_col,
            g.sections[sid].grid_row,
            round(g.sections[sid].offset_x + g.sections[sid].bbox_x, 3),
            round(g.sections[sid].offset_y + g.sections[sid].bbox_y, 3),
        )
        for sid in sorted(g.sections)
    )


def test_component_placement_is_deterministic_in_process() -> None:
    """Repeated layouts of the same graph yield identical placement.

    Each ``compute_layout`` re-parses the source, so any reliance on a
    set/dict iteration order seeded once per process would surface as a
    drifting signature across repeats within a single interpreter.
    """
    sigs = {_placement_signature(_layout(_FOLDING_TRUNK_PLUS_DISC)) for _ in range(8)}
    assert len(sigs) == 1


def test_component_placement_is_deterministic_across_hash_seeds() -> None:
    """Placement is identical under different ``PYTHONHASHSEED`` values.

    Set iteration over section-id strings is hash-randomised, so a path that
    leaks set order into grid assignment would place a trunk section in a
    different ``grid_row`` (and stack the components differently) depending on
    the seed.  Run the layout in fresh subprocesses under several explicit
    seeds and assert the signature is identical every time.
    """
    import os

    repo_root = Path(__file__).resolve().parent.parent
    script = textwrap.dedent(
        """
        import sys
        from nf_metro.parser.mermaid import parse_metro_mermaid
        from nf_metro.layout.engine import compute_layout

        text = sys.stdin.read()
        g = parse_metro_mermaid(text)
        compute_layout(g)
        for sid in sorted(g.sections):
            s = g.sections[sid]
            print(
                f"{sid} {s.grid_col} {s.grid_row} "
                f"{round(s.offset_x + s.bbox_x, 3)} "
                f"{round(s.offset_y + s.bbox_y, 3)}"
            )
        """
    )

    outputs = set()
    for seed in ("0", "1", "2", "7", "42", "1234"):
        env = dict(os.environ)
        env["PYTHONHASHSEED"] = seed
        proc = subprocess.run(
            [sys.executable, "-c", script],
            input=_FOLDING_TRUNK_PLUS_DISC,
            capture_output=True,
            text=True,
            cwd=repo_root,
            env=env,
        )
        assert proc.returncode == 0, proc.stderr
        outputs.add(proc.stdout)

    assert len(outputs) == 1, "section placement varied across PYTHONHASHSEED values"
