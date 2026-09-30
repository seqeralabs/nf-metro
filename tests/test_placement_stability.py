"""Placement stability under intra-section edits (#1082).

An intra-section edit -- adding a station inside one section without touching
the inter-section DAG -- must not re-grid any *other* section.  Inter-section
placement is a function of the inter-section DAG and explicit directives, not
of intra-section size.

The one intentional coupling is the serpentine fold budget: a row that
overflows ``fold_threshold`` wraps onto the next row, and where the fold lands
shifts as the row widens.  That is the fold feature working as designed, so the
corpus property here suppresses folding (a very large ``max_station_columns``)
and asserts that, with the fold budget removed, a leaf edit moves nothing --
placement is then purely DAG-driven.

Separately, a branch-column fold must never strand a consumer *behind* its
producer when it does fire (the #1074 fragility, fixed by #1080).  The
``branch_fold_stability`` fixture sits one leaf below its fold threshold;
adding that leaf must leave the downstream consumer on its forward cell.
"""

from __future__ import annotations

import re
import warnings
from pathlib import Path

import pytest

from nf_metro.layout.auto_layout import _internal_station_depths
from nf_metro.layout.engine import compute_layout
from nf_metro.parser.mermaid import parse_metro_mermaid
from nf_metro.parser.model import MetroGraph, Section

EXAMPLES_DIR = Path(__file__).parent.parent / "examples"
TOPOLOGIES_DIR = EXAMPLES_DIR / "topologies"
TOPOLOGY_FILES = sorted(TOPOLOGIES_DIR.glob("*.mmd"))
TOPOLOGY_IDS = [f.stem for f in TOPOLOGY_FILES]

# A column budget high enough that no corpus fixture folds, isolating
# placement from the intentional width-driven serpentine fold.
_SUPPRESS_FOLD = 100_000

Cells = dict[str, tuple[int, int]]


def _section_cells(graph: MetroGraph) -> Cells:
    return {sid: (s.grid_col, s.grid_row) for sid, s in graph.sections.items()}


def _layout(text: str, max_station_columns: int | None = None) -> MetroGraph:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        graph = parse_metro_mermaid(text, max_station_columns=max_station_columns)
        compute_layout(graph)
    return graph


def _deepest_real_anchor(graph: MetroGraph, section: Section) -> tuple[str, str] | None:
    """The deepest non-port internal station and a line it carries.

    Returns ``None`` when the section has no internal edge to extend (a lone
    station or a port-only section), so nothing can be appended on a line.
    """
    depths = _internal_station_depths(graph, section.id)
    if not depths:
        return None
    line_by_target = {e.target: e.line_id for e in section.internal_edges}
    line_by_source = {e.source: e.line_id for e in section.internal_edges}
    for sid, _ in sorted(depths.items(), key=lambda kv: -kv[1]):
        st = graph.stations.get(sid)
        if st is None or st.is_port:
            continue
        line_id = line_by_target.get(sid) or line_by_source.get(sid)
        if line_id is not None:
            return sid, line_id
    return None


def test_internal_station_depths_uses_longest_predecessor_path() -> None:
    graph = parse_metro_mermaid(
        """\
%%metro line: main | Main | #ff0000
graph LR
    subgraph work [Work]
        start[Start]
        short[Short]
        long_1[Long 1]
        long_2[Long 2]
        join[Join]
        tail[Tail]
        start -->|main| short
        short -->|main| join
        start -->|main| long_1
        long_1 -->|main| long_2
        long_2 -->|main| join
        join -->|main| tail
    end
"""
    )

    depths = _internal_station_depths(graph, "work")

    assert depths is not None
    assert depths["join"] == 3
    assert depths["tail"] == 4


def _inject_leaf(text: str, section_id: str, anchor: str, line_id: str) -> str:
    """Append ``anchor -->|line_id| __stability_leaf`` inside ``section_id``.

    The Mermaid subset has no nested subgraphs, so the first ``end`` after the
    section header closes that section -- the leaf seats just before it.
    """
    out: list[str] = []
    in_section = False
    injected = False
    for line in text.splitlines():
        if re.match(rf"\s*subgraph\s+{re.escape(section_id)}\b", line):
            in_section = True
        if in_section and not injected and re.match(r"\s*end\s*$", line):
            out.append("        __stability_leaf[Leaf]")
            out.append(f"        {anchor} -->|{line_id}| __stability_leaf")
            in_section = False
            injected = True
        out.append(line)
    assert injected, f"section {section_id!r} not found for leaf injection"
    return "\n".join(out) + "\n"


def _cells_after_leaf(
    text: str,
    section_id: str,
    anchor: str,
    line_id: str,
    max_station_columns: int | None,
) -> Cells:
    """Grid cells after appending a leaf to ``section_id`` and re-laying out."""
    return _section_cells(
        _layout(_inject_leaf(text, section_id, anchor, line_id), max_station_columns)
    )


def _moved_sections(base: Cells, after: Cells, edited: str) -> list[str]:
    """Sections other than ``edited`` whose grid cell changed."""
    return [
        f"{edited!r} edit moved {other!r}: {cell} -> {after.get(other)}"
        for other, cell in base.items()
        if other != edited and after.get(other) != cell
    ]


@pytest.mark.parametrize("path", TOPOLOGY_FILES, ids=TOPOLOGY_IDS)
def test_intra_section_edit_does_not_regrid_others(path: Path) -> None:
    """With folding suppressed, a leaf edit never re-grids another section."""
    text = path.read_text()
    base = _layout(text, _SUPPRESS_FOLD)
    base_cells = _section_cells(base)

    violations: list[str] = []
    for sid, section in base.sections.items():
        if section.is_implicit or section.is_hidden:
            continue
        anchor = _deepest_real_anchor(base, section)
        if anchor is None:
            continue
        after = _cells_after_leaf(text, sid, *anchor, _SUPPRESS_FOLD)
        violations += _moved_sections(base_cells, after, sid)

    assert not violations, "\n".join(violations)


def test_branch_fold_edit_keeps_consumer_forward() -> None:
    """A leaf on a wide branch section must not strand its consumer (#1074).

    ``branch_fold_stability`` pins ``fold_threshold: 6`` and sits one leaf
    below it: appending a station to the wide ``survey`` branch tips the column
    over budget.  Pre-#1080 that folded the column and dropped ``report`` onto a
    backward return row, behind its ``align`` producer; placement must keep
    ``report`` on its original, forward cell.
    """
    text = (TOPOLOGIES_DIR / "branch_fold_stability.mmd").read_text()

    base = _layout(text)
    base_cells = _section_cells(base)
    anchor = _deepest_real_anchor(base, base.sections["survey"])
    assert anchor is not None

    after = _cells_after_leaf(text, "survey", *anchor, None)

    regridded = _moved_sections(base_cells, after, "survey")
    assert not regridded, "\n".join(regridded)
    assert after["report"][0] > after["align"][0], (
        "consumer 'report' is not forward of its producer 'align' after the edit"
    )
