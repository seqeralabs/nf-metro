"""Unit tests for the ``_is_fold_section`` row-fold predicate."""

from __future__ import annotations

import pytest

from nf_metro.layout.phases._common import _is_fold_section
from nf_metro.parser.model import Section


def _section(direction: str, grid_row_span: int) -> Section:
    section = Section(id="s", name="S")
    section.direction = direction
    section.grid_row_span = grid_row_span
    return section


@pytest.mark.parametrize(
    "direction, grid_row_span, is_fold",
    [
        ("LR", 1, False),
        ("RL", 1, False),
        ("LR", 2, True),  # multi-row span folds regardless of flow
        ("RL", 3, True),
        ("TB", 1, True),  # vertical flow is a fold even in a single row
        ("BT", 1, True),
        ("TB", 2, True),
    ],
)
def test_is_fold_section(direction: str, grid_row_span: int, is_fold: bool) -> None:
    assert _is_fold_section(_section(direction, grid_row_span)) is is_fold
