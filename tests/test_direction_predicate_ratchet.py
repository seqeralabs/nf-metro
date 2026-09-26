"""Ratchet on direction-keyed predicates in the geometry-bearing packages.

``test_tb_branch_ratchet`` counts bare ``"TB"`` references.  The other way a
heuristic gets keyed to an orientation is a membership test or lookup table over
a *proper subset* of the flow directions -- ``direction in ("LR", "RL")``,
``_FLIP_HORIZONTAL = {"LR": "RL", "RL": "LR"}`` -- or a predicate that hides
the same distinction behind a name such as ``tb_positive_fan``.  Each such
subset is some axis property spelled out by hand, and a partial spelling is how
one orientation of a shape ends up on a different code path from another.

``AxisFrame`` (``layout/geometry.py``) already supplies the properties:

===================  =========================================================
subset               the property it is standing in for
===================  =========================================================
``{LR, RL}``         flow runs along X: ``lanes_run_along_y(direction)``
``{TB, BT}``         flow runs along Y: ``lanes_run_along_x(direction)``
``{RL, BT}``         flow is reversed: ``AxisFrame.flow_sign(direction) < 0``
``{TB, RL}``         two properties at once, and needs splitting: a horizontal
                     flow reversed on X, or a vertical flow whose lanes fan to
                     -X (``AxisFrame.secondary_sign_for(direction) < 0``)
===================  =========================================================

``layout/geometry.py`` is exempt: it is where those accessors are defined, so
its direction literals are the vocabulary rather than a use of it.

The bounds are upper ones. Lower them whenever a call site migrates onto the
accessors, and never raise them.
"""

from __future__ import annotations

import ast
import re
from functools import cache
from pathlib import Path
from typing import Callable

from nf_metro.parser.model import FLOW_DIRECTIONS

_SRC = Path(__file__).resolve().parents[1] / "src" / "nf_metro"

# Packages whose code makes geometric decisions and so can be orientation-keyed.
_SCANNED_PACKAGES = ("layout", "parser", "render")

# The accessors' own definitions, not a use of them.
_EXEMPT = frozenset({"layout/geometry.py"})

# Exact counts, not slack: ``test_direction_predicate_baselines_are_current``
# asserts equality, so a site migrating onto AxisFrame has to lower the number
# here in the same change.  Never raise them.
_LITERAL_BASELINE = 59
_NAMED_BASELINE = 264

_FLOWS = frozenset(FLOW_DIRECTIONS)
_FLOW_NAME_TOKENS = frozenset(flow.lower() for flow in _FLOWS)
_AXIS_NAME_TOKENS = frozenset({"horizontal", "vertical"})
_FLOW_NAME_PHRASES = (
    ("left", "to", "right"),
    ("right", "to", "left"),
    ("top", "to", "bottom"),
    ("bottom", "to", "top"),
)


def _direction_subset(node: ast.expr) -> frozenset[str]:
    """The flow directions a container literal is keyed on, if it is keyed only on them.

    A dict contributes its keys, a set/tuple/list its elements.  Returns empty
    for anything holding a non-direction string, so an unrelated lookup table
    that happens to contain ``"LR"`` is not counted.
    """
    if isinstance(node, ast.Dict):
        elements = [k for k in node.keys if k is not None]
    elif isinstance(node, (ast.Set, ast.Tuple, ast.List)):
        elements = list(node.elts)
    else:
        return frozenset()
    values = {
        e.value
        for e in elements
        if isinstance(e, ast.Constant) and isinstance(e.value, str)
    }
    if not values or not values <= _FLOWS:
        return frozenset()
    return frozenset(values)


def _is_partial(subset: frozenset[str]) -> bool:
    """A subset that discriminates between orientations without covering them all."""
    return 2 <= len(subset) < len(_FLOWS)


def _qualified_name(node: ast.expr) -> str | None:
    """The dotted identifier named by *node*, excluding calls and expressions."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _qualified_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    return None


def _name_is_direction_keyed(name: str) -> bool:
    """Whether *name* explicitly describes a flow direction or axis."""
    tokens = tuple(part.lower() for part in re.findall(r"[A-Za-z]+", name))
    if set(tokens) & (_FLOW_NAME_TOKENS | _AXIS_NAME_TOKENS):
        return True
    return any(
        tokens[index : index + len(phrase)] == phrase
        for phrase in _FLOW_NAME_PHRASES
        for index in range(len(tokens) - len(phrase) + 1)
    )


def _direction_origin(
    node: ast.expr, aliases: dict[str, frozenset[str]]
) -> tuple[str, frozenset[str]] | None:
    """The displayed and canonical direction name carried by *node*."""
    if isinstance(node, ast.Call):
        return _direction_origin(node.func, aliases)
    name = _qualified_name(node)
    if name is None:
        return None
    if _name_is_direction_keyed(name):
        return (name, frozenset({name}))
    if isinstance(node, ast.Name) and (origins := aliases.get(name)):
        origin_label = ", ".join(sorted(origins))
        return (f"{name} (alias of {origin_label})", origins)
    return None


def _direction_named(
    node: ast.expr, aliases: dict[str, frozenset[str]] | None = None
) -> str | None:
    """A direction-keyed identifier carried by *node*, if one is explicit."""
    result = _direction_origin(node, aliases or {})
    return result[0] if result else None


def _assigned_names(node: ast.expr) -> set[str]:
    """Plain names bound by an assignment target."""
    if isinstance(node, ast.Name):
        return {node.id}
    if isinstance(node, (ast.Tuple, ast.List)):
        return {name for element in node.elts for name in _assigned_names(element)}
    return set()


def _literal_sites_from_source(
    source: str, filename: str = "<unknown>"
) -> list[tuple[int, str]]:
    """Every literal direction-keyed table or membership test in source."""
    found: list[tuple[int, str]] = []
    tree = ast.parse(source, filename=filename)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
            subset = _direction_subset(node.value)
            if _is_partial(subset):
                targets = (
                    node.targets if isinstance(node, ast.Assign) else [node.target]
                )
                names = ", ".join(
                    name
                    for target in targets
                    for name in sorted(_assigned_names(target))
                )
                found.append(
                    (node.lineno, f"table {names or '<unnamed>'} over {sorted(subset)}")
                )
        elif isinstance(node, ast.Compare) and any(
            isinstance(op, (ast.In, ast.NotIn)) for op in node.ops
        ):
            for comparator in node.comparators:
                subset = _direction_subset(comparator)
                if _is_partial(subset):
                    found.append((node.lineno, f"membership in {sorted(subset)}"))
    return sorted(found)


def _literal_sites_in(path: Path) -> list[tuple[int, str]]:
    return _literal_sites_from_source(path.read_text(), filename=str(path))


class _NamedSiteCollector(ast.NodeVisitor):
    """Find named sites while tracking simple aliases in lexical scopes."""

    def __init__(self) -> None:
        self.aliases: list[dict[str, frozenset[str]]] = [{}]
        self.scope_kinds = ["module"]
        self.conditional_depth = 0
        self.found: set[tuple[int, str]] = set()

    @property
    def scope(self) -> dict[str, frozenset[str]]:
        return self.aliases[-1]

    def _bind(self, targets: list[ast.expr], value: ast.expr) -> None:
        origin = _direction_origin(value, self.scope)
        for target in targets:
            for name in _assigned_names(target):
                if origin:
                    previous = self.scope.get(name, frozenset())
                    self.scope[name] = (
                        previous | origin[1] if self.conditional_depth else origin[1]
                    )
                elif not self.conditional_depth:
                    self.scope.pop(name, None)

    def _invalidate(self, targets: list[ast.expr]) -> None:
        if self.conditional_depth:
            return
        for target in targets:
            for name in _assigned_names(target):
                self.scope.pop(name, None)

    def _scope_without_arguments(
        self, arguments: ast.arguments
    ) -> dict[str, frozenset[str]]:
        enclosing = next(
            aliases
            for aliases, kind in reversed(list(zip(self.aliases, self.scope_kinds)))
            if kind != "class"
        )
        local = enclosing.copy()
        names = [
            *arguments.posonlyargs,
            *arguments.args,
            *arguments.kwonlyargs,
        ]
        if arguments.vararg:
            names.append(arguments.vararg)
        if arguments.kwarg:
            names.append(arguments.kwarg)
        for argument in names:
            local.pop(argument.arg, None)
        return local

    def visit_Assign(self, node: ast.Assign) -> None:
        self.visit(node.value)
        self._bind(node.targets, node.value)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self.visit(node.annotation)
        if node.value is not None:
            self.visit(node.value)
            self._bind([node.target], node.value)
        else:
            self._invalidate([node.target])

    def visit_NamedExpr(self, node: ast.NamedExpr) -> None:
        self.visit(node.value)
        self._bind([node.target], node.value)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        for imported in node.names:
            bound = imported.asname or imported.name
            origin = ".".join(part for part in (node.module, imported.name) if part)
            if _name_is_direction_keyed(origin):
                previous = self.scope.get(bound, frozenset())
                imported_origins = frozenset({origin})
                self.scope[bound] = (
                    previous | imported_origins
                    if self.conditional_depth
                    else imported_origins
                )
            elif not self.conditional_depth:
                self.scope.pop(bound, None)

    def visit_Import(self, node: ast.Import) -> None:
        for imported in node.names:
            bound = imported.asname or imported.name.split(".", maxsplit=1)[0]
            if not self.conditional_depth:
                self.scope.pop(bound, None)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        self.visit(node.value)
        self._invalidate([node.target])

    def visit_Delete(self, node: ast.Delete) -> None:
        self._invalidate(node.targets)

    def _visit_for(self, node: ast.For | ast.AsyncFor) -> None:
        self.visit(node.iter)
        self.conditional_depth += 1
        self._invalidate([node.target])
        for statement in [*node.body, *node.orelse]:
            self.visit(statement)
        self.conditional_depth -= 1

    def visit_For(self, node: ast.For) -> None:
        self._visit_for(node)

    def visit_AsyncFor(self, node: ast.AsyncFor) -> None:
        self._visit_for(node)

    def visit_If(self, node: ast.If) -> None:
        self.visit(node.test)
        self.conditional_depth += 1
        for statement in [*node.body, *node.orelse]:
            self.visit(statement)
        self.conditional_depth -= 1

    def visit_While(self, node: ast.While) -> None:
        self.visit(node.test)
        self.conditional_depth += 1
        for statement in [*node.body, *node.orelse]:
            self.visit(statement)
        self.conditional_depth -= 1

    def _visit_try(self, node: ast.Try | ast.TryStar) -> None:
        self.conditional_depth += 1
        for statement in [*node.body, *node.handlers, *node.orelse]:
            self.visit(statement)
        self.conditional_depth -= 1
        for statement in node.finalbody:
            self.visit(statement)

    def visit_Try(self, node: ast.Try) -> None:
        self._visit_try(node)

    def visit_TryStar(self, node: ast.TryStar) -> None:
        self._visit_try(node)

    def visit_Match(self, node: ast.Match) -> None:
        self.visit(node.subject)
        self.conditional_depth += 1
        for case in node.cases:
            if case.guard is not None:
                self.visit(case.guard)
            for statement in case.body:
                self.visit(statement)
        self.conditional_depth -= 1

    def _visit_with(self, node: ast.With | ast.AsyncWith) -> None:
        for item in node.items:
            self.visit(item.context_expr)
            if item.optional_vars is not None:
                self._invalidate([item.optional_vars])
        for statement in node.body:
            self.visit(statement)

    def visit_With(self, node: ast.With) -> None:
        self._visit_with(node)

    def visit_AsyncWith(self, node: ast.AsyncWith) -> None:
        self._visit_with(node)

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if node.type is not None:
            self.visit(node.type)
        if node.name is not None and not self.conditional_depth:
            self.scope.pop(node.name, None)
        for statement in node.body:
            self.visit(statement)

    def visit_Compare(self, node: ast.Compare) -> None:
        if any(isinstance(op, (ast.In, ast.NotIn)) for op in node.ops):
            for comparator in node.comparators:
                if name := _direction_named(comparator, self.scope):
                    self.found.add(
                        (node.lineno, f"membership in named predicate {name}")
                    )
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        if name := _direction_named(node.func, self.scope):
            self.found.add((node.lineno, f"call to direction-keyed helper {name}"))
        self.generic_visit(node)

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        for decorator in node.decorator_list:
            self.visit(decorator)
        for default in [*node.args.defaults, *node.args.kw_defaults]:
            if default is not None:
                self.visit(default)
        if node.returns is not None:
            self.visit(node.returns)

        self.aliases.append(self._scope_without_arguments(node.args))
        self.scope_kinds.append("function")
        for statement in node.body:
            self.visit(statement)
        self.aliases.pop()
        self.scope_kinds.pop()
        if not self.conditional_depth:
            self.scope.pop(node.name, None)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        self.aliases.append(self._scope_without_arguments(node.args))
        self.scope_kinds.append("function")
        self.visit(node.body)
        self.aliases.pop()
        self.scope_kinds.pop()

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        for decorator in node.decorator_list:
            self.visit(decorator)
        for base in node.bases:
            self.visit(base)
        for keyword in node.keywords:
            self.visit(keyword.value)
        self.aliases.append(self.scope.copy())
        self.scope_kinds.append("class")
        for statement in node.body:
            self.visit(statement)
        self.aliases.pop()
        self.scope_kinds.pop()
        if not self.conditional_depth:
            self.scope.pop(node.name, None)


def _named_sites_from_source(
    source: str, filename: str = "<unknown>"
) -> list[tuple[int, str]]:
    """Every direction-named membership or helper call in source."""
    collector = _NamedSiteCollector()
    collector.visit(ast.parse(source, filename=filename))
    return sorted(collector.found)


def _named_sites_in(path: Path) -> list[tuple[int, str]]:
    return _named_sites_from_source(path.read_text(), filename=str(path))


def _scan(
    detector: Callable[[Path], list[tuple[int, str]]],
) -> dict[str, list[tuple[int, str]]]:
    out: dict[str, list[tuple[int, str]]] = {}
    for package in _SCANNED_PACKAGES:
        for path in sorted((_SRC / package).rglob("*.py")):
            relative = path.relative_to(_SRC).as_posix()
            if relative in _EXEMPT:
                continue
            if sites := detector(path):
                out[relative] = sites
    return out


@cache
def direction_predicate_sites() -> dict[str, list[tuple[int, str]]]:
    """Map each scanned module to literal direction-container sites."""
    return _scan(_literal_sites_in)


@cache
def named_direction_predicate_sites() -> dict[str, list[tuple[int, str]]]:
    """Map each scanned module to direction-keyed named sites."""
    return _scan(_named_sites_in)


def _breakdown(sites: dict[str, list[tuple[int, str]]]) -> str:
    return "\n  ".join(
        f"{len(found):>3}  {module}"
        for module, found in sorted(sites.items(), key=lambda kv: -len(kv[1]))
    )


def test_no_new_literal_direction_keyed_predicates() -> None:
    total = sum(len(v) for v in direction_predicate_sites().values())

    # Guard against the counter silently matching nothing (packages moved, the
    # AST walk broken): the engine genuinely carries dozens of these today.
    assert total >= 40, (
        f"expected many direction-keyed predicates, found {total} - the counter "
        "may be broken or the packages restructured"
    )

    assert total <= _LITERAL_BASELINE, (
        "literal direction-keyed predicate count rose to "
        f"{total} (baseline {_LITERAL_BASELINE}).\n"
        "A heuristic that needs to know a section's axis or flow sense should ask "
        "AxisFrame (layout/geometry.py: lanes_run_along_x/y, AxisFrame.flow_sign, "
        "AxisFrame.secondary_sign_for) rather than testing membership of a subset "
        "of the flow directions. A partial subset is how one orientation of a "
        f"geometry reaches a different code path from another.\n  "
        f"{_breakdown(direction_predicate_sites())}"
    )


def test_no_new_named_direction_keyed_predicates() -> None:
    sites = named_direction_predicate_sites()
    total = sum(len(found) for found in sites.values())

    assert total <= _NAMED_BASELINE, (
        f"named direction-keyed site count rose to {total} "
        f"(baseline {_NAMED_BASELINE}).\n"
        "A direction embedded in a helper or collection name hides the same "
        "orientation split as a literal. Express the property through AxisFrame "
        "or add an orientation-neutral classifier instead.\n  "
        f"{_breakdown(sites)}"
    )


def test_direction_predicate_baselines_are_current() -> None:
    literal_total = sum(len(v) for v in direction_predicate_sites().values())
    named_total = sum(len(v) for v in named_direction_predicate_sites().values())
    assert literal_total == _LITERAL_BASELINE
    assert named_total == _NAMED_BASELINE


def test_exempt_modules_exist() -> None:
    """The exemption names a real module, so a rename cannot silently widen scope."""
    for relative in _EXEMPT:
        assert (_SRC / relative).is_file(), f"exempt module {relative} not found"


def test_geometry_defines_the_accessors_the_ratchet_points_at() -> None:
    """The suggested replacements exist, so the failure message stays actionable."""
    from nf_metro.layout import geometry

    for name in ("lanes_run_along_x", "lanes_run_along_y", "AxisFrame"):
        assert hasattr(geometry, name), f"geometry.{name} is missing"
    for name in ("flow_sign", "secondary_sign_for", "axes_for_direction"):
        assert hasattr(geometry.AxisFrame, name), f"AxisFrame.{name} is missing"


def test_named_direction_membership_is_counted() -> None:
    source = (
        "def selected(section_id, tb_positive_fan):\n"
        "    return section_id in tb_positive_fan\n"
    )

    assert _named_sites_from_source(source) == [
        (2, "membership in named predicate tb_positive_fan")
    ]


def test_named_direction_predicate_call_is_counted() -> None:
    source = (
        "def selected(section):\n"
        "    if is_tb_positive_fan(section):\n"
        "        return True\n"
        "    return False\n"
    )

    assert _named_sites_from_source(source) == [
        (2, "call to direction-keyed helper is_tb_positive_fan")
    ]


def test_named_direction_classifier_call_is_counted() -> None:
    source = "def selected(graph):\n    return tb_positive_fan_sections(graph)\n"

    assert _named_sites_from_source(source) == [
        (2, "call to direction-keyed helper tb_positive_fan_sections")
    ]


def test_direction_named_receiver_is_reported_in_full() -> None:
    source = (
        "def selected(tb_positive_fan, section_id):\n"
        "    tb_positive_fan.add(section_id)\n"
    )

    assert _named_sites_from_source(source) == [
        (2, "call to direction-keyed helper tb_positive_fan.add")
    ]


def test_semantic_direction_names_are_counted() -> None:
    source = (
        "def selected(section_id, vertical_positive_fan):\n"
        "    if section_id in vertical_positive_fan:\n"
        "        return left_to_right_sections()\n"
        "    return horizontal_return_sections()\n"
    )

    assert _named_sites_from_source(source) == [
        (2, "membership in named predicate vertical_positive_fan"),
        (3, "call to direction-keyed helper left_to_right_sections"),
        (4, "call to direction-keyed helper horizontal_return_sections"),
    ]


def test_direction_named_helper_alias_is_counted() -> None:
    source = (
        "def selected(graph):\n"
        "    classifier = tb_positive_fan_sections\n"
        "    return classifier(graph)\n"
    )

    assert _named_sites_from_source(source) == [
        (
            3,
            "call to direction-keyed helper "
            "classifier (alias of tb_positive_fan_sections)",
        )
    ]


def test_imported_direction_named_helper_alias_is_counted() -> None:
    source = (
        "from package import vertical_positive_fan_sections as classifier\n"
        "\n"
        "def selected(graph):\n"
        "    return classifier(graph)\n"
    )

    assert _named_sites_from_source(source) == [
        (
            4,
            "call to direction-keyed helper "
            "classifier (alias of package.vertical_positive_fan_sections)",
        )
    ]


def test_direction_named_result_alias_membership_is_counted() -> None:
    source = (
        "def selected(graph, section_id):\n"
        "    fan = vertical_positive_fan_sections(graph)\n"
        "    return section_id in fan\n"
    )

    assert _named_sites_from_source(source) == [
        (2, "call to direction-keyed helper vertical_positive_fan_sections"),
        (
            3,
            "membership in named predicate "
            "fan (alias of vertical_positive_fan_sections)",
        ),
    ]


def test_alias_tracking_respects_class_scope() -> None:
    source = (
        "from package import vertical_classifier as classifier\n"
        "class Example:\n"
        "    classifier = plain_classifier\n"
        "    def selected(self):\n"
        "        return classifier()\n"
    )

    assert _named_sites_from_source(source) == [
        (
            5,
            "call to direction-keyed helper "
            "classifier (alias of package.vertical_classifier)",
        )
    ]


def test_conditional_rebinding_preserves_possible_direction_alias() -> None:
    source = (
        "from package import vertical_classifier as classifier\n"
        "if flag:\n"
        "    classifier = plain_classifier\n"
        "classifier(graph)\n"
    )

    assert _named_sites_from_source(source) == [
        (
            4,
            "call to direction-keyed helper "
            "classifier (alias of package.vertical_classifier)",
        )
    ]


def test_loop_target_preserves_possible_direction_alias() -> None:
    source = (
        "from package import vertical_classifier as classifier\n"
        "for classifier in classifiers:\n"
        "    pass\n"
        "classifier()\n"
    )

    assert _named_sites_from_source(source) == [
        (
            4,
            "call to direction-keyed helper "
            "classifier (alias of package.vertical_classifier)",
        )
    ]


def test_exception_target_preserves_possible_direction_alias() -> None:
    source = (
        "def select(graph):\n"
        "    classifier = vertical_classifier\n"
        "    try:\n"
        "        work()\n"
        "    except Exception as classifier:\n"
        "        pass\n"
        "    return classifier(graph)\n"
    )

    assert _named_sites_from_source(source) == [
        (
            7,
            "call to direction-keyed helper classifier (alias of vertical_classifier)",
        )
    ]


def test_annotated_direction_table_is_counted() -> None:
    source = '_REVERSALS: dict[str, str] = {"LR": "RL", "RL": "LR"}\n'

    assert _literal_sites_from_source(source) == [
        (1, "table _REVERSALS over ['LR', 'RL']")
    ]
