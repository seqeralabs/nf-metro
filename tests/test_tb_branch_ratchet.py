"""Ratchet on one-off TB branches in the layout package (policy in CONTRACT.md).

Counts ``"TB"`` literals and ``.TB`` attribute accesses across
``src/nf_metro/layout/`` and fails if the total rises above the baseline. A
heuristic needing TB awareness should migrate onto the ``AxisFrame`` primitive
(``layout/geometry.py``) rather than add another ``direction == "TB"`` branch.
"""

from __future__ import annotations

import ast
from pathlib import Path

_LAYOUT_DIR = Path(__file__).resolve().parents[1] / "src/nf_metro/layout"

# Lower this (never raise it) when a heuristic migrates onto AxisFrame.
_BASELINE_TB_BRANCHES = 26


def _count_in_file(path: Path) -> int:
    tree = ast.parse(path.read_text(), filename=str(path))
    count = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and node.value == "TB":
            count += 1
        elif isinstance(node, ast.Attribute) and node.attr == "TB":
            count += 1
    return count


def count_tb_branches() -> dict[str, int]:
    """Map each layout module (relative path) to its TB-reference count."""
    return {
        str(path.relative_to(_LAYOUT_DIR)): n
        for path in sorted(_LAYOUT_DIR.rglob("*.py"))
        if (n := _count_in_file(path))
    }


def test_no_new_tb_branches() -> None:
    per_file = count_tb_branches()
    total = sum(per_file.values())

    # Guard against the counter silently matching nothing (modules moved, AST
    # walk broken): the engine genuinely carries dozens of TB references today.
    assert total >= 20, (
        f"expected many TB references, found {total} - the counter may be "
        "broken or the layout package restructured"
    )

    breakdown = "\n  ".join(f"{n:>3}  {name}" for name, n in per_file.items())
    assert total <= _BASELINE_TB_BRANCHES, (
        f"TB-branch count rose to {total} (baseline {_BASELINE_TB_BRANCHES}).\n"
        "A heuristic needing TB awareness is the trigger to convert it to the "
        "AxisFrame primary/secondary vocabulary (layout/geometry.py), not to add "
        f'another one-off `direction == "TB"` branch (CONTRACT.md).\n  {breakdown}'
    )


def test_baseline_is_current() -> None:
    """Baseline equals the live count, so headroom can't accumulate after a drop."""
    total = sum(count_tb_branches().values())
    assert total == _BASELINE_TB_BRANCHES, (
        f"TB-branch count is {total} but the baseline is {_BASELINE_TB_BRANCHES}. "
        "If you migrated a heuristic onto AxisFrame (count dropped), lower the "
        "baseline to match; if the count rose, see test_no_new_tb_branches."
    )
