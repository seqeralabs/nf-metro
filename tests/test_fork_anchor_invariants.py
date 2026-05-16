"""Invariants for `_equalize_fork_groups` track distribution.

Issue #317: fan-out columns with multiple distinct primary lines and a
common predecessor can have their members distributed lopsidedly below
the predecessor when `_equalize_fork_groups` anchors on the topmost
station ("group[0]") and walks downward by primary-line priority.

These tests assert that when a fan-out group shares a common
predecessor (the "trunk" feeding the column), the column's Y
center-of-mass is close to the predecessor's Y rather than being
heavily biased above/below. Symmetric distribution around the trunk is
what produces clean horizontal trunk routing without dipping V detours
on the orphan lines that land furthest from the trunk.
"""

from __future__ import annotations

from pathlib import Path

import networkx as nx
import pytest

from nf_metro.layout.engine import compute_layout
from nf_metro.parser.mermaid import parse_metro_mermaid

ROOT = Path(__file__).resolve().parent.parent
FIXTURE_DIRS = [
    ROOT / "examples",
    ROOT / "examples" / "topologies",
    ROOT / "tests" / "fixtures",
]

_Y_BIAS_TOL = 75.0  # ~one track unit at default y_spacing


def _load(name: str):
    for d in FIXTURE_DIRS:
        p = d / f"{name}.mmd"
        if p.exists():
            text = p.read_text()
            g = parse_metro_mermaid(text)
            compute_layout(g)
            return g
    raise FileNotFoundError(name)


def _shared_pred_fanout_columns(g):
    """Yield (section, col_x, sids, pred_id) for fan-out columns with:
    - >=2 stations,
    - >=2 distinct primary lines (so `_equalize_fork_groups` fires),
    - a non-empty common predecessor set in the section subgraph.
    """
    line_priority = {lid: i for i, lid in enumerate(g.lines)}
    G = nx.DiGraph()
    for e in g.edges:
        G.add_edge(e.source, e.target)

    for section in g.sections.values():
        if section.direction not in ("LR", "RL"):
            continue
        port_ids = set(section.entry_ports) | set(section.exit_ports)
        cols: dict[float, list[str]] = {}
        for sid in section.station_ids:
            if sid in port_ids:
                continue
            st = g.stations.get(sid)
            if st is None or st.is_port:
                continue
            cols.setdefault(round(st.x, 1), []).append(sid)
        for col_x, sids in cols.items():
            if len(sids) < 2:
                continue
            primaries: set[str] = set()
            for sid in sids:
                lines = g.station_lines(sid)
                if lines:
                    primaries.add(
                        min(lines, key=lambda ln: line_priority.get(ln, 1_000_000))
                    )
            if len(primaries) < 2:
                continue

            pred_sets = [set(G.predecessors(s)) for s in sids]
            if not pred_sets[0]:
                continue
            if not all(p == pred_sets[0] for p in pred_sets):
                continue
            pred = next(iter(pred_sets[0]))
            if pred not in g.stations:
                continue
            yield section, col_x, sids, pred


@pytest.mark.parametrize(
    "fixture",
    [
        "variantbenchmarking",
        "variantbenchmarking_auto",
        "differentialabundance",
        "epitopeprediction",
        "rnaseq_sections",
        "da_pipeline",
    ],
)
def test_fork_group_symmetric_about_trunk(fixture):
    """Fan-out columns with a common predecessor should be distributed
    symmetrically about the predecessor's Y.

    Asymmetry means orphan-line siblings (the ones whose primary line
    is far from the predecessor's primary line) land far from the trunk
    Y. Their inter-section route then has to climb back to trunk Y,
    producing a visible V-shaped detour (issue #317).
    """
    g = _load(fixture)
    violations = []
    for section, col_x, sids, pred in _shared_pred_fanout_columns(g):
        pred_y = g.stations[pred].y
        ys = [g.stations[s].y for s in sids]
        mean_y = sum(ys) / len(ys)
        bias = mean_y - pred_y
        if abs(bias) > _Y_BIAS_TOL:
            violations.append(
                f"  section={section.id} col_x={col_x} pred={pred} "
                f"pred_y={pred_y:.1f} mean_y={mean_y:.1f} bias={bias:+.1f}"
            )

    assert not violations, (
        f"Fan-out column not symmetric about trunk in {fixture}:\n"
        + "\n".join(violations)
    )
