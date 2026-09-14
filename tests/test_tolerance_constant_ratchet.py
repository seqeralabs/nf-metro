"""Ratchet on tolerance values spelled out where a named constant already holds them.

``layout/constants.py`` is the vocabulary of geometry tolerances: every band the
engine measures against ("same coordinate", "same assigned row", the guard
slack) is named and documented there. A pass or guard that writes one of those
numbers out at its own call site instead reads the same band by coincidence, so
tuning the named constant leaves the copy behind and a guard can drift away from
the pass it guards.

A tolerance whose value differs from every named one is a genuinely independent
band, and is not counted: the class this bounds is duplication, not the
existence of a local tolerance.

``constants.py`` itself is exempt, since that is where the named values are
defined rather than used.

The bound is exact, not slack: ``test_tolerance_literal_baseline_is_current``
asserts equality, so clearing a site has to lower the number here in the same
change. Never raise it.
"""

from __future__ import annotations

import ast
import operator
from collections.abc import Callable
from pathlib import Path
from typing import NamedTuple

_LAYOUT_DIR = Path(__file__).resolve().parents[1] / "src" / "nf_metro" / "layout"

_VOCABULARY_FILE = "constants.py"

_BASELINE = 0


class ToleranceLiteral(NamedTuple):
    """One tolerance-named binding whose literal duplicates a named constant."""

    name: str
    value: float
    line: int
    constants: tuple[str, ...]


def _is_tolerance_name(name: str) -> bool:
    return "tol" in name.lower()


_BINARY_OPS: dict[type[ast.operator], Callable[[float, float], float]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}

_UNARY_OPS: dict[type[ast.unaryop], Callable[[float], float]] = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}


def _folded_number(node: ast.expr | None) -> float | None:
    """The value of *node* when it folds to a number over constants alone.

    A margin derived from the vocabulary (``OFFSET_STEP + 1.0``) states how its
    value relates to it, so any expression referencing a name yields None.
    Arithmetic over constants alone is the value itself however it is spelled,
    so it folds rather than escaping.
    """
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            return None
        return float(node.value)
    if isinstance(node, ast.UnaryOp):
        unary = _UNARY_OPS.get(type(node.op))
        operand = _folded_number(node.operand)
        if unary is None or operand is None:
            return None
        return float(unary(operand))
    if isinstance(node, ast.BinOp):
        binary = _BINARY_OPS.get(type(node.op))
        left = _folded_number(node.left)
        right = _folded_number(node.right)
        if binary is None or left is None or right is None:
            return None
        try:
            return float(binary(left, right))
        except (ArithmeticError, ValueError):
            return None
    return None


def _assigned_names(node: ast.Assign | ast.AnnAssign) -> list[str]:
    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
    return [t.id for t in targets if isinstance(t, ast.Name)]


def tolerance_vocabulary(source: str) -> dict[float, tuple[str, ...]]:
    """Map each module-level tolerance constant's value to the names holding it."""
    vocabulary: dict[float, list[str]] = {}
    for node in ast.parse(source).body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        value = _folded_number(node.value)
        if value is None:
            continue
        for name in _assigned_names(node):
            if _is_tolerance_name(name):
                vocabulary.setdefault(value, []).append(name)
    return {value: tuple(names) for value, names in vocabulary.items()}


def duplicated_tolerance_literals(
    source: str,
    vocabulary: dict[float, tuple[str, ...]],
) -> dict[str, ToleranceLiteral]:
    """Tolerance-named bindings in *source* whose literal a constant already holds.

    Keys are ``<enclosing qualname>::<name>`` for a binding inside a function or
    class and the bare name at module level, so a site keeps one key as the file
    around it changes.
    """
    found: dict[str, ToleranceLiteral] = {}

    def visit(node: ast.AST, scope: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.Assign, ast.AnnAssign)):
                value = _folded_number(child.value)
                constants = vocabulary.get(value) if value is not None else None
                if value is not None and constants:
                    for name in _assigned_names(child):
                        if _is_tolerance_name(name):
                            key = f"{scope}::{name}" if scope else name
                            found[key] = ToleranceLiteral(
                                name, value, child.lineno, constants
                            )
            inner = scope
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                inner = f"{scope}.{child.name}" if scope else child.name
            visit(child, inner)

    visit(ast.parse(source), "")
    return found


def _layout_sites() -> dict[str, ToleranceLiteral]:
    vocabulary = tolerance_vocabulary((_LAYOUT_DIR / _VOCABULARY_FILE).read_text())
    sites: dict[str, ToleranceLiteral] = {}
    for path in sorted(_LAYOUT_DIR.rglob("*.py")):
        relative = path.relative_to(_LAYOUT_DIR)
        if str(relative) == _VOCABULARY_FILE:
            continue
        for key, site in duplicated_tolerance_literals(
            path.read_text(), vocabulary
        ).items():
            sites[f"{relative}::{key}"] = site
    return sites


def _breakdown(sites: dict[str, ToleranceLiteral]) -> str:
    return "\n  ".join(
        f"{key}:{site.line} = {site.value} (see {', '.join(site.constants)})"
        for key, site in sorted(sites.items())
    )


def test_no_tolerance_literals_duplicating_named_constants() -> None:
    sites = _layout_sites()
    assert len(sites) <= _BASELINE, (
        f"tolerance literals duplicating a named constant rose to {len(sites)} "
        f"(baseline {_BASELINE}). Import the constant, or give the site a value "
        "and a docstring of its own so it is visibly an independent band.\n  "
        f"{_breakdown(sites)}"
    )


def test_tolerance_literal_baseline_is_current() -> None:
    sites = _layout_sites()
    assert len(sites) == _BASELINE, (
        f"tolerance-literal baseline is {_BASELINE}, live count is {len(sites)}; "
        "lower the baseline in the same change that clears a site"
    )


def test_vocabulary_collects_tolerance_constants_by_value() -> None:
    source = """
COORD_TOLERANCE: float = 1.0
EDGE_CONNECT_TOL = 1.0
CURVE_RADIUS = 10.0
GUARD_TOLERANCE = 5.0
DERIVED_TOLERANCE = GUARD_TOLERANCE
"""

    assert tolerance_vocabulary(source) == {
        1.0: ("COORD_TOLERANCE", "EDGE_CONNECT_TOL"),
        5.0: ("GUARD_TOLERANCE",),
    }


def test_function_local_duplicate_is_found_with_its_scope() -> None:
    source = """
def _guard_padding(graph):
    tol = 1.0
    return tol
"""

    assert duplicated_tolerance_literals(source, {1.0: ("COORD_TOLERANCE",)}) == {
        "_guard_padding::tol": ToleranceLiteral("tol", 1.0, 3, ("COORD_TOLERANCE",))
    }


def test_constant_only_arithmetic_folds_to_the_duplicated_value() -> None:
    source = """
_SUM_TOL = 1.0 + 0.0
_PRODUCT_TOL = 1.0 * 1
_NEGATED_TOL = -(-1.0)
_PARENTHESISED_TOL = (1.00)
"""

    assert duplicated_tolerance_literals(source, {1.0: ("COORD_TOLERANCE",)}) == {
        "_SUM_TOL": ToleranceLiteral("_SUM_TOL", 1.0, 2, ("COORD_TOLERANCE",)),
        "_PRODUCT_TOL": ToleranceLiteral("_PRODUCT_TOL", 1.0, 3, ("COORD_TOLERANCE",)),
        "_NEGATED_TOL": ToleranceLiteral("_NEGATED_TOL", 1.0, 4, ("COORD_TOLERANCE",)),
        "_PARENTHESISED_TOL": ToleranceLiteral(
            "_PARENTHESISED_TOL", 1.0, 5, ("COORD_TOLERANCE",)
        ),
    }


def test_independent_value_and_named_reference_are_not_counted() -> None:
    source = """
_SLOPE_TOL = 0.12
_LATERAL_TOL = COORD_TOLERANCE
_COINCIDE_TOL = OFFSET_STEP + 1.0
_SCALED_TOL = 0.25 * (OFFSET_STEP + 0.0)
_MIN_SPAN = 1.0
"""

    assert duplicated_tolerance_literals(source, {1.0: ("COORD_TOLERANCE",)}) == {}
