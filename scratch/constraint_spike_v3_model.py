"""Standalone wrapper for the constraint-solver spike (issue #351).

Delegates to src/nf_metro/layout/constraint_solver_v2.py for the model
itself. Provides a `solve()` entry point that runs the engine and then
the solver, so scratch comparison scripts can use it without going
through compute_layout.
"""

from __future__ import annotations

from typing import Optional

from nf_metro.layout import compute_layout
from nf_metro.layout.constraint_solver_v2 import apply_to_graph
from nf_metro.layout.engine import compute_min_y_spacing
from nf_metro.parser.model import MetroGraph


def solve(g: MetroGraph, y_spacing: Optional[float] = None) -> dict:
    """Run the engine then override Ys via the solver. Mutates g."""
    if y_spacing is None:
        y_spacing = compute_min_y_spacing(g)
    compute_layout(g, y_spacing=y_spacing, validate=False)
    return apply_to_graph(g, y_spacing)
