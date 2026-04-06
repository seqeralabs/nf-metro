"""Tests for section numbering by visual reading order (#217).

After layout, sections are numbered by flow sweep then (grid_col,
grid_row).  Each left-to-right or right-to-left run is one sweep,
with TB fold sections belonging to the sweep they terminate.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nf_metro.layout.engine import compute_layout
from nf_metro.parser.mermaid import parse_metro_mermaid

EXAMPLES_DIR = Path(__file__).resolve().parent.parent / "examples"


def _load(name: str):
    """Parse and lay out an example pipeline."""
    text = (EXAMPLES_DIR / f"{name}.mmd").read_text()
    g = parse_metro_mermaid(text)
    compute_layout(g)
    return g


# Module-scoped fixtures to avoid redundant compute_layout calls.


@pytest.fixture(scope="module")
def variantprioritization():
    return _load("variantprioritization")


@pytest.fixture(scope="module")
def variantbenchmarking():
    return _load("variantbenchmarking")


@pytest.fixture(scope="module")
def rnaseq_auto():
    return _load("rnaseq_auto")


@pytest.fixture(scope="module")
def asymmetric_tree():
    return _load("topologies/asymmetric_tree")


class TestSectionNumberingOrder:
    """Section numbers should follow visual reading order."""

    def test_numbers_are_sequential(self, variantprioritization):
        """Section numbers should be 1..N with no gaps."""
        numbers = sorted(s.number for s in variantprioritization.sections.values())
        assert numbers == list(range(1, len(variantprioritization.sections) + 1))

    def test_all_examples_sequential(self):
        """Every example with sections should have sequential numbering."""
        for mmd_path in sorted(EXAMPLES_DIR.glob("*.mmd")):
            text = mmd_path.read_text()
            g = parse_metro_mermaid(text)
            if not g.sections:
                continue
            compute_layout(g)
            numbers = sorted(s.number for s in g.sections.values())
            assert numbers == list(range(1, len(g.sections) + 1)), (
                f"{mmd_path.name}: section numbers not sequential: {numbers}"
            )

    def test_within_sweep_columns_increase(
        self, variantprioritization, variantbenchmarking, rnaseq_auto
    ):
        """Within each flow sweep, numbers increase left-to-right.

        Sections at the same column should be numbered top-to-bottom.
        """
        for name, g in [
            ("variantprioritization", variantprioritization),
            ("variantbenchmarking", variantbenchmarking),
            ("rnaseq_auto", rnaseq_auto),
        ]:
            secs = sorted(g.sections.values(), key=lambda s: s.number)
            for i in range(len(secs) - 1):
                a, b = secs[i], secs[i + 1]
                if a.grid_col == b.grid_col:
                    assert a.grid_row <= b.grid_row, (
                        f"{name}: #{a.number} {a.name} (row {a.grid_row}) "
                        f"before #{b.number} {b.name} (row {b.grid_row}) "
                        f"at col {a.grid_col}"
                    )

    def test_fold_return_row_numbered_after_forward_row(self, rnaseq_auto):
        """RL sections after a fold should have higher numbers than all
        LR sections in the preceding sweep."""
        lr_nums = [
            s.number
            for s in rnaseq_auto.sections.values()
            if s.direction in ("LR", "TB")
        ]
        rl_nums = [
            s.number for s in rnaseq_auto.sections.values() if s.direction == "RL"
        ]
        if lr_nums and rl_nums:
            assert min(rl_nums) > max(lr_nums), (
                f"RL sections {rl_nums} should all be > LR/TB sections {lr_nums}"
            )

    def test_asymmetric_top_row_sequential(self, asymmetric_tree):
        """In asymmetric_tree, the top row should be numbered sequentially."""
        top_row = sorted(
            (s for s in asymmetric_tree.sections.values() if s.grid_row == 0),
            key=lambda s: s.grid_col,
        )
        nums = [s.number for s in top_row]
        for i in range(len(nums) - 1):
            assert nums[i] < nums[i + 1], f"Top row numbers not increasing: {nums}"
