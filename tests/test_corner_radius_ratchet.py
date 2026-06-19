"""Ratchet: every corner radius in the routing handlers comes from corners.py.

#508 centralised all corner-radius computation onto the ``routing/corners.py``
helper family (``reference_anchored_radius`` as the single formula).  This test
machine-enforces that it *stays* centralised: every value that reaches a
``RoutedPath.curve_radii`` slot in the routing handler modules must trace back -
directly, through a same-scope name/alias hop, or through a tuple-unpack - to a
call of one of those helpers.  Inline ``base +/- offset`` arithmetic (or a bare
``ctx.curve_radius`` / ``CURVE_RADIUS`` literal) feeding a radius slot fails,
whether written inline in the ``curve_radii=[...]`` list or hidden in an
intermediate variable.

Without this, a future handler could write ``curve_radii=[ctx.curve_radius +
off]`` again and nothing would catch the un-centralised, potentially
non-concentric nesting (issue #515, follow-up to #508).
"""

from __future__ import annotations

import ast
import textwrap
from pathlib import Path

import pytest

_ROUTING_DIR = Path(__file__).resolve().parents[1] / "src/nf_metro/layout/routing"

# The handler family that route_edges dispatches through.  Corner radii are
# written across these modules; every one must trace to a corners.py helper.
ROUTING_PATHS = [
    _ROUTING_DIR / name
    for name in (
        "core.py",
        "context.py",
        "inter_section_handlers.py",
        "tb_handlers.py",
        "intra_handlers.py",
        "postprocess.py",
        "normalize.py",
    )
]

# corners.py entry points that legitimately *produce* (or clamp) a corner
# radius.  ``reference_anchored_radius`` is the single underlying formula; the
# others delegate to it.  ``corner_outside_sign``/``reversed_offset`` return a
# sign/offset (an *input* to a radius), not a radius, so are intentionally out.
APPROVED_RADIUS_HELPERS = frozenset(
    {
        "corner_radius",
        "l_shape_radii",
        "concentric_corner_radius",
        "concentric_corner_radius_at",
        "reference_anchored_radius",
        "resolve_curve_radii",
    }
)


def _is_curve_radii_slot(target: ast.expr) -> bool:
    """True for a ``<...>.curve_radii[idx]`` subscript assignment target."""
    return (
        isinstance(target, ast.Subscript)
        and isinstance(target.value, ast.Attribute)
        and target.value.attr == "curve_radii"
    )


def _called_name(call: ast.Call) -> str | None:
    """Return the simple name of a call's callee (``foo`` or ``mod.foo``)."""
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


class _Scope:
    """Lexical scope: maps a local name to the RHS expressions bound to it.

    A tuple-unpack target (``_, r_first, r_second = l_shape_radii(...)``) records
    the source ``Call`` so the unpacked radius traces to its producing helper.
    """

    def __init__(self, parent: "_Scope | None") -> None:
        self.parent = parent
        self.bindings: dict[str, list[ast.expr]] = {}

    def bind(self, name: str, value: ast.expr) -> None:
        self.bindings.setdefault(name, []).append(value)

    def lookup(self, name: str) -> list[ast.expr] | None:
        scope: _Scope | None = self
        while scope is not None:
            if name in scope.bindings:
                return scope.bindings[name]
            scope = scope.parent
        return None


def _collect_assignments(scope: _Scope, body: list[ast.stmt]) -> None:
    """Record every name binding in *body* at any depth (if/for/with/...),
    without crossing into a nested function's own scope."""
    for stmt in body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue  # a nested def/class owns a separate scope
        for node in _walk_same_scope(stmt):
            _record_targets(scope, node)


def _walk_same_scope(node: ast.AST) -> list[ast.AST]:
    """All descendants of *node* that share its function scope (stops at a
    nested def / lambda / comprehension boundary, which introduce new scopes)."""
    out: list[ast.AST] = [node]
    for child in ast.iter_child_nodes(node):
        if isinstance(
            child,
            (
                ast.FunctionDef,
                ast.AsyncFunctionDef,
                ast.Lambda,
                ast.ListComp,
                ast.SetComp,
                ast.DictComp,
                ast.GeneratorExp,
            ),
        ):
            continue
        out.extend(_walk_same_scope(child))
    return out


def _record_targets(scope: _Scope, node: ast.AST) -> None:
    """Bind names assigned in *node*, modelling plain, annotated and augmented
    assignment.  ``r += off`` binds ``r`` to a ``BinOp`` so it never resolves to
    a helper - augmenting a radius is arithmetic by definition, so the slot is
    rejected whatever the operand."""
    if isinstance(node, ast.Assign):
        targets, value = node.targets, node.value
    elif isinstance(node, ast.AnnAssign):
        if node.value is None:
            return  # bare annotation, no binding
        targets, value = [node.target], node.value
    elif isinstance(node, ast.AugAssign):
        targets = [node.target]
        value = ast.BinOp(left=node.target, op=node.op, right=node.value)
    else:
        return
    for target in targets:
        if isinstance(target, ast.Name):
            scope.bind(target.id, value)
        elif isinstance(target, (ast.Tuple, ast.List)):
            for elt in target.elts:
                if isinstance(elt, ast.Name):
                    scope.bind(elt.id, value)


def _resolves_to_helper(
    node: ast.expr, scope: _Scope, seen_names: frozenset[str] = frozenset()
) -> bool:
    """True if *node* (a value feeding ``curve_radii``) derives from a helper.

    Recurses through containers (list / list-comp / if-expr / tuple), name and
    alias hops (resolved against the lexical *scope*), and subscripts of helper
    results.  A ``Call`` must target an approved helper; a ``BinOp`` / numeric
    constant / bare attribute (``ctx.curve_radius``) or an unbound name
    (``CURVE_RADIUS`` module constant) is a raw radius and fails.

    *seen_names* breaks alias cycles (``a = b; b = a``); it tracks visited names
    only, so a node shared by sibling branches (one helper ``Call`` unpacked
    into several names) is judged independently down each branch.
    """
    if isinstance(node, ast.Call):
        return _called_name(node) in APPROVED_RADIUS_HELPERS
    if isinstance(node, ast.Name):
        if node.id in seen_names:
            return False  # alias cycle: never anchored on a helper
        bindings = scope.lookup(node.id)
        if not bindings:
            return False  # module constant / param / undefined: raw radius
        deeper = seen_names | {node.id}
        return all(_resolves_to_helper(b, scope, deeper) for b in bindings)
    if isinstance(node, (ast.List, ast.Tuple)):
        return all(_resolves_to_helper(e, scope, seen_names) for e in node.elts)
    if isinstance(node, ast.ListComp):
        return _resolves_to_helper(node.elt, scope, seen_names)
    if isinstance(node, ast.IfExp):
        return _resolves_to_helper(
            node.body, scope, seen_names
        ) and _resolves_to_helper(node.orelse, scope, seen_names)
    if isinstance(node, (ast.Subscript, ast.Starred)):
        inner = node.value
        return isinstance(inner, ast.expr) and _resolves_to_helper(
            inner, scope, seen_names
        )
    return False


class _RadiusSlotFinder(ast.NodeVisitor):
    """Find every value feeding a ``curve_radii`` slot, paired with its scope."""

    def __init__(self) -> None:
        self.scope = _Scope(None)
        # (lineno, value_node, scope) for each radius slot.
        self.slots: list[tuple[int, ast.expr, _Scope]] = []

    def _enter(self, node: ast.AST) -> None:
        child = _Scope(self.scope)
        _collect_assignments(child, node.body)  # type: ignore[attr-defined]
        prev, self.scope = self.scope, child
        self.generic_visit(node)
        self.scope = prev

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._enter(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._enter(node)

    def visit_Call(self, node: ast.Call) -> None:
        for kw in node.keywords:
            if kw.arg == "curve_radii":
                self.slots.append((kw.value.lineno, kw.value, self.scope))
        self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        # ``rp.curve_radii[idx] = expr`` mutations.
        for target in node.targets:
            if _is_curve_radii_slot(target):
                self.slots.append((node.value.lineno, node.value, self.scope))
        self.generic_visit(node)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        # ``rp.curve_radii[idx] += expr`` mutations are arithmetic by
        # definition; record the augmented value so the slot is rejected.
        if _is_curve_radii_slot(node.target):
            value = ast.BinOp(left=node.target, op=node.op, right=node.value)
            self.slots.append((node.value.lineno, value, self.scope))
        self.generic_visit(node)


def _find_radius_slots(path: Path) -> list[tuple[int, ast.expr, _Scope]]:
    # Module-level assignments share the root scope.
    tree = ast.parse(path.read_text(), filename=str(path))
    finder = _RadiusSlotFinder()
    _collect_assignments(finder.scope, tree.body)
    finder.visit(tree)
    return finder.slots


def test_every_curve_radius_traces_to_a_corners_helper() -> None:
    per_file = {path: _find_radius_slots(path) for path in ROUTING_PATHS}
    total = sum(len(slots) for slots in per_file.values())

    # Guard against the finder silently matching nothing (e.g. modules moved).
    assert total >= 5, (
        f"expected to find many curve_radii sites, found {total} - "
        "the finder may be broken or the routing modules restructured"
    )

    violations = [
        f"{path.name}:{lineno}"
        for path, slots in per_file.items()
        for lineno, value, scope in slots
        if not _resolves_to_helper(value, scope)
    ]
    assert not violations, (
        "curve_radii values not traceable to a corners.py helper at "
        f"{sorted(violations)}. Every corner radius must "
        "flow through one of "
        f"{sorted(APPROVED_RADIUS_HELPERS)} (#508/#515); wrap a bare base radius "
        "as reference_anchored_radius(0.0, base) rather than using it raw or "
        "writing inline base +/- offset arithmetic."
    )


# --- the guard, verified against its own positive/negative cases ------------
#
# Each case is a complete function whose single ``curve_radii=[...]`` slot is
# resolved by the same finder used above, so these exercise the real
# scope/alias/container/tuple-unpack machinery rather than a hand-built AST.

_ACCEPTED = {
    "direct-helper-call": """
        def f():
            return P(curve_radii=[corner_radius(o, m, base_radius=b)])
    """,
    "wrapped-base-literal": """
        def f():
            return P(curve_radii=[reference_anchored_radius(0.0, b)])
    """,
    "tuple-unpack": """
        def f():
            _, r_first, r_second = l_shape_radii(i, n, v)
            return P(curve_radii=[r_first, r_second])
    """,
    "alias-hop": """
        def f():
            r = concentric_corner_radius(a, b, dx)
            rr = r
            return P(curve_radii=[rr])
    """,
    "annotated-helper-assignment": """
        def f():
            r: float = corner_radius(o, m)
            return P(curve_radii=[r])
    """,
    "list-comp-via-name": """
        def f():
            radii = [reference_anchored_radius(0.0, b) for _ in items]
            return P(curve_radii=radii)
    """,
    "if-expr-branches": """
        def f():
            return P(curve_radii=[
                corner_radius(o, m) if cond else reference_anchored_radius(0.0, b)
            ])
    """,
}

_REJECTED = {
    "inline-arithmetic": """
        def f():
            return P(curve_radii=[ctx.curve_radius + off])
    """,
    "bare-attribute-literal": """
        def f():
            return P(curve_radii=[ctx.curve_radius])
    """,
    "module-constant-literal": """
        def f():
            return P(curve_radii=[CURVE_RADIUS])
    """,
    "arithmetic-hidden-in-variable": """
        def f():
            r = ctx.curve_radius + off
            return P(curve_radii=[r])
    """,
    "augmented-assignment-on-helper-result": """
        def f():
            r = corner_radius(o, m)
            r += off
            return P(curve_radii=[r])
    """,
    "annotated-arithmetic": """
        def f():
            r: float = ctx.curve_radius + off
            return P(curve_radii=[r])
    """,
    "non-helper-call": """
        def f():
            return P(curve_radii=[min(ctx.curve_radius, half)])
    """,
}


def _slot_resolves(source: str) -> bool:
    tree = ast.parse(textwrap.dedent(source))
    finder = _RadiusSlotFinder()
    finder.visit(tree)
    assert len(finder.slots) == 1, f"want one slot, got {len(finder.slots)}"
    _, value, scope = finder.slots[0]
    return _resolves_to_helper(value, scope)


@pytest.mark.parametrize("source", _ACCEPTED.values(), ids=_ACCEPTED.keys())
def test_helper_derived_radii_accepted(source: str) -> None:
    assert _slot_resolves(source), "helper-derived radius should be accepted"


@pytest.mark.parametrize("source", _REJECTED.values(), ids=_REJECTED.keys())
def test_raw_radii_rejected(source: str) -> None:
    assert not _slot_resolves(source), "raw radius should be rejected"


# ---------------------------------------------------------------------------
# Ratchet: handlers pass only ``ctx.curve_radius`` as a bundle base radius
# ---------------------------------------------------------------------------

# The bundle builder / centreline-template entry points.  Each owns its corner
# anchoring and leg-fit, so the only ``base_radius`` a handler may hand it is the
# global floor ``ctx.curve_radius`` -- never a value pre-bumped by the bundle's
# half-width or shrunk to fit a leg.
_BUILDER_ENTRYPOINTS = frozenset(
    {
        "build_concentric_bundle",
        "build_tapered_bundle",
        "route_along",
        "route_tapered",
        "route_hvh_tapered",
        "route_straight",
    }
)
# Positional index of ``base_radius`` for builders that accept it positionally
# (the ``route_*`` templates take it keyword-only).
_BASE_RADIUS_POS = {"build_concentric_bundle": 2, "build_tapered_bundle": 3}


def _is_curve_radius(node: ast.expr) -> bool:
    """True for the ``ctx.curve_radius`` attribute access."""
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "curve_radius"
        and isinstance(node.value, ast.Name)
        and node.value.id == "ctx"
    )


def _base_radius_arg(call: ast.Call) -> ast.expr | None:
    """The ``base_radius`` value passed to a builder *call*, keyword or positional."""
    for kw in call.keywords:
        if kw.arg == "base_radius":
            return kw.value
    pos = _BASE_RADIUS_POS.get(_called_name(call) or "")
    if pos is not None and len(call.args) > pos:
        return call.args[pos]
    return None


def test_handler_bundle_base_radius_is_curve_radius_only() -> None:
    """Every builder call in the routing handlers passes ``ctx.curve_radius``.

    The builder anchors each corner on the bundle's innermost-of-turn line, so a
    handler need only supply the floor; a hand-computed base (half-width bump,
    leg-fit shrink, or a recomputed lead-in) is exactly the fragility this
    ratchet keeps out of the handlers.  Any ``base_radius`` argument to a builder
    entry point that is not literally ``ctx.curve_radius`` fails.
    """
    offenders: list[str] = []
    for path in ROUTING_PATHS:
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = _called_name(node)
            if name not in _BUILDER_ENTRYPOINTS:
                continue
            base = _base_radius_arg(node)
            if base is not None and not _is_curve_radius(base):
                offenders.append(
                    f"{path.name}:{node.lineno} {name}(base_radius={ast.unparse(base)})"
                )
    assert not offenders, (
        "Builder base_radius must be ctx.curve_radius (the builder owns "
        "anchoring and leg-fit):\n  " + "\n  ".join(offenders)
    )
