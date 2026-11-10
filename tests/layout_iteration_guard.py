"""AST detector for iteration and look-ahead in ``src/nf_metro/layout/``.

The layout decides each coordinate once, from plan-time inputs; later phases
consume the decision.  This module finds code that works the other way: it runs
a phase or writer speculatively, runs one again, or repairs a decision a phase
already made.  It reads source with :mod:`ast` only and imports nothing from
``nf_metro``.

Registry
--------
Every rule keys on a registry of phase and writer entry points that is computed
from the source on each run:

* **phase tables**: an ``engine.py`` function that calls ``_snap`` (the stage
  snapshot call) at least three times is a table of layout stages, and so is
  the ``routing/offsets.py`` function that calls both ``_build_offset_ctx`` and
  ``_hold_decided_orders``.  Tables are registered themselves.
* **table phases**: inside a table, every statement-level call (its result is
  discarded, so it acts by side effect) whose first positional argument is
  ``graph`` or ``ctx``, and every function name passed as an argument
  (``_run_placement(graph, validate, "4.9", fn, ...)``), is a phase.  Calls to
  ``_snap``, snapshot recorders, guards, ``_assert*``/``_hold*``/``_validate*``/
  ``_cache*`` checkers and ``_build*`` constructors are not writers and are
  skipped.
* **offset writers**: in ``routing/offsets.py`` a function whose first parameter
  is ``ctx`` and that stores into or mutates ``ctx.offsets``
  (``ctx.offsets[k] = v``, ``ctx.offsets.update(...)``) is a writer, and so is
  any such function that passes its ``ctx`` to a writer.
* **pass runners**: an ``engine.py`` or ``routing/offsets.py`` function that
  calls a table or runner of its own module is a runner, to a fixed point.
  Nested functions count, so a ``_layout_pass`` closure over ``_compute_layout_scaled``
  is a runner.
* **solver entry points**: every function in ``routing/corridor_cohorts.py``,
  nested ones included, whose name starts with ``solve``, ``_solve`` or ``seat``.
  A nested one is matched only inside its own module.
* **delegate parameters**: a parameter that receives a registered function at
  some call site (``apply_geometric_bypass(graph, _layout_pass)``,
  ``_run_placement(graph, validate, stage, fn)``, ``partial(writer, ...)``) is
  registered inside the function that declares it, so calling it counts as
  calling the phase.
* **routing tables**: the ``routing/core.py`` function that calls
  ``assert_exit_turn_snapshot`` at least three times sequences the passes that
  run on emitted routes.
* **later passes**: the routing table, the ``routing/core.py`` functions that
  call it (``route_edges`` and its variants), the public runners of
  ``routing/offsets.py`` (``compute_station_offsets``), and every pass the
  routing table runs on the emitted routes.  They run after placement, so a call
  from outside the table, such as a planner running a pass on a copy of the
  routes, reads a later stage.
* **pinned passes**: the allowlist's ``pinned_later_passes`` names the passes
  the routing table runs; they stay registered after a call leaves the table,
  so a planner that runs one is reported whether or not the table runs it.
  ``pinned_nested_passes`` names writer sub-passes that a pass runs on routes,
  such as ``_bundle_same_destination_tails``; they are registered the same way.  The
  tests require every table pass to be pinned and every pin to name a function
  that exists and is called somewhere.

Guard modules (``phases/guards.py``, ``routing/invariants.py``) and functions
named ``_guard*``, ``check_*`` or ``assert_*`` only check, so they are not
scanned.

Detection rules
---------------
``lookahead-copy``
    A function binds a scratch copy of phase state and passes it, as the first
    argument or receiver, to a registered call, so the call runs on the copy
    instead of the live state.  A scratch copy is ``deepcopy``/``copy.copy`` of
    anything, ``dataclasses.replace(x, offsets=...)`` or
    ``replace(x, station_offsets=...)`` (a context holding hypothetical
    offsets), or ``dict(...)``/``ChainMap(...)`` of an expression naming
    ``offsets``.  The copy may be bound to a name first, or passed inline.  A
    copy kept as a before/after snapshot and handed over as a later argument is
    not a run on the copy.

``scratch-offsets``
    ``replace(x, offsets=...)`` or ``replace(x, station_offsets=...)`` flowing
    into a call that is not registered, in a function with no ``lookahead-copy``
    hit.  A context holding hypothetical offsets exists to evaluate something
    on them; the callee is a planner or measure of the hypothetical state.  One
    hit per function.

``retry-loop``
    A registered call inside a loop where the call's inputs do not depend on the
    loop (the same work runs again), or where the loop can ``break``/``return``
    after consuming the call's result (alternatives are tried until one
    succeeds).  A ``for`` call depends on the loop when an argument, or an
    enclosing ``with`` header between the loop and the call, names the loop
    target or a name assigned from it.  A ``while`` call depends on the loop when
    it names something the loop body assigns, as a worklist walk does.  A loop
    that applies a phase once per item and always continues is not a retry.  A
    call that fills a name once under ``if name is None`` runs once, and so does
    a returned call whose inputs do not depend on the loop.  A returned call that
    reads the loop target, or any returned call in a loop over a literal tuple or
    list, takes the first alternative that passes: it is a fallback ladder.  A
    ``break`` inside an inner loop is not the outer loop's exit.

``rerun``
    A registered runner, solver or delegate is called from two sites of one
    function that are not mutually exclusive.  Sites are exclusive when they are
    on different arms of an ``if``, when the earlier one is returned, or when
    the earlier one sits in an arm that ends in ``return``/``raise``/
    ``continue``/``break``.  Inside a phase table, any phase appearing from two
    non-exclusive sites is a rerun: the later occurrence redoes or repairs the
    first.  A table phase passed to a wrapper such as ``_run_placement`` counts
    as the phase, not the wrapper.

``later-pass-read``
    A function other than a table or runner calls a later pass.
    The caller reads a product of stages that have not run yet, or runs routing
    to measure placement.  Inside a route pass only a call to another table or
    pinned pass is reported, which is a pass run both nested and on its own;
    composing its own sub-passes is not.

``repair-named``
    A table phase whose name declares that it undoes or redoes an earlier
    decision (``REPAIR_MARKERS``).  The rule is a name heuristic and the only
    one that is not structural, so every allowlist entry it produces is a judged
    hit.

``route-postpass``
    In a routing table, a call that takes the emitted ``routes`` list and is
    not an assertion, validation or snapshot.  Each such pass rewrites routes
    after emission, so it overrides a planned route or leaves it alone only
    because an assertion after it says so.  The AST cannot tell a pass that
    touches only legacy routes from one that moves planned geometry, so each
    entry is a pass to remove or to prove inert.

``calls-lookahead``
    A function that calls one reported as ``lookahead-copy``, directly or
    through a chain of such callers that ends at a registered phase.  These
    are the decision sites that skip or accept a decision according to what the
    copy run showed (refusal by trial).

``named-retry``
    A layout function whose own name says it retries, restarts or vetoes
    (``RETRY_MARKERS``).  This catches a retry loop around callees the registry
    does not hold, such as a pure query.

Limits
------
Resolution is by bare name, so a registered name reused by an unrelated function
in another module matches both.  Retry and fixed-point loops around callees that
are neither registered nor named for it, and repair passes with a neutral name,
are not detected.  Also not detected: a loop over alternative arguments whose
success is read from graph state rather than from the call's result
(``for pitch in ...: _layout_once(graph, pitch)`` followed by ``if ok(graph):
break``), a phase passed as a lambda, a retry by recursion, a trial run on the
live state followed by a rollback, and a copy made by a helper function
(``_corner_sizing_copy``).  A ``.copy()`` method is read as a scratch copy only
when its receiver names ``offsets``, ``graph`` or ``ctx``.  A ``for`` loop that
exits early after consuming a phase's result is reported even when it is a plain
search over items rather than a ladder of alternatives.  A loop that only searches
(``for p in orders: if ok(p): break``) and calls the phase once after it is not
detected.  A loop over a tuple of phases (``for step in (phase_a, phase_b):
step(graph)``) is not detected.  A pin deleted while its pass has callers drops
their rows; review checks that each deleted pin names a pass with no callers.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

LAYOUT_ROOT = Path(__file__).resolve().parent.parent / "src" / "nf_metro" / "layout"

# Modules by role.  ``routing`` is route planning and emission; ``placement`` is
# section and station placement.  Every layout module has exactly one role.
ROUTING_MODULES = frozenset(
    {
        "departure_order.py",
        "envelope_settlement.py",
        "fan_geometry.py",
        "fan_ordering.py",
        "fan_plans.py",
        "route_plan.py",
        "route_reservations.py",
        "route_topology.py",
        "seam_topology.py",
        "settlement_demand.py",
    }
)
PLACEMENT_MODULES = frozenset(
    {
        "__init__.py",
        "auto_layout.py",
        "constants.py",
        "engine.py",
        "geometric_bypass.py",
        "geometry.py",
        "labels.py",
        "layers.py",
        "ordering.py",
        "pass_metrics.py",
        "phase_state.py",
        "rail_mode.py",
        "section_placement.py",
    }
)

ENGINE = "engine.py"
OFFSETS = "routing/offsets.py"
SOLVER = "routing/corridor_cohorts.py"

CHECKER_MODULES = frozenset({"phases/guards.py", "routing/invariants.py"})
CHECKER_PREFIXES = ("_guard", "guard_", "check_", "assert_", "_assert")
NON_WRITER_PREFIXES = (
    "_guard",
    "_assert",
    "_hold",
    "_validate",
    "_cache",
    "_build",
    "_snapshot",
)
NON_WRITER_NAMES = frozenset({"_snap"})
ROUTING = "routing/core.py"
MIN_SNAPSHOT_CHECKS_FOR_ROUTING_TABLE = 3
POSTPASS_SKIP_PREFIXES = ("assert_", "validate_", "snapshot_", "planned_")
MIN_SNAPS_FOR_TABLE = 3
SOLVER_PREFIXES = ("solve", "_solve", "seat")
OFFSET_STATE_KWARGS = frozenset({"offsets", "station_offsets"})
COPY_CALLS = frozenset({"deepcopy", "copy"})
COPY_WRAPPERS = frozenset({"dict", "ChainMap"})
COPY_RECEIVER_TERMS = ("offsets", "graph", "ctx")
MUTATORS = frozenset({"update", "pop", "setdefault", "clear", "popitem"})
BUILTIN_CALLS = frozenset(
    {"tuple", "list", "set", "dict", "len", "sorted", "min", "max", "sum", "any", "all"}
)
REPAIR_MARKERS = (
    "recompact",
    "restore",
    "reconcile",
    "resnap",
    "reanchor",
    "re_anchor",
    "rederive",
    "retrofit",
    "reflow",
    "refit",
    "realign",
    "heal",
    "repair",
)
RETRY_MARKERS = ("retry", "rerun", "re_run", "restart", "veto")

RULES = (
    "calls-lookahead",
    "route-postpass",
    "lookahead-copy",
    "scratch-offsets",
    "retry-loop",
    "rerun",
    "later-pass-read",
    "repair-named",
    "named-retry",
)

_DEFS = (ast.FunctionDef, ast.AsyncFunctionDef)
_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)
_LOOPS = (ast.For, ast.AsyncFor, ast.While)
_TERMINATORS = (ast.Return, ast.Raise, ast.Continue, ast.Break)


@dataclass(frozen=True)
class Hit:
    rule: str
    site: str
    callee: str
    line: int
    detail: str = ""

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.rule, self.site, self.callee)


@dataclass
class Func:
    module: str
    qualname: str
    node: ast.FunctionDef | ast.AsyncFunctionDef
    params: list[str]

    @property
    def name(self) -> str:
        return self.node.name

    @property
    def site(self) -> str:
        return f"{self.module}:{self.qualname}"


@dataclass
class Registry:
    tables: set[str] = field(default_factory=set)
    table_phases: set[str] = field(default_factory=set)
    wrappers: set[str] = field(default_factory=set)
    offset_writers: set[str] = field(default_factory=set)
    runners: set[str] = field(default_factory=set)
    solver: set[str] = field(default_factory=set)
    solver_local: set[str] = field(default_factory=set)
    routing_tables: set[str] = field(default_factory=set)
    table_postpasses: set[str] = field(default_factory=set)
    postpasses: set[str] = field(default_factory=set)
    nested_passes: set[str] = field(default_factory=set)
    entry_passes: set[str] = field(default_factory=set)
    later_passes: set[str] = field(default_factory=set)
    delegates: dict[str, set[str]] = field(default_factory=dict)

    @property
    def names(self) -> set[str]:
        return (
            self.tables
            | self.table_phases
            | self.offset_writers
            | self.runners
            | self.solver
            | self.solver_local
            | self.later_passes
        )


def module_role(module: str) -> str | None:
    """``"routing"`` or ``"placement"`` for a layout module path, else None."""
    if module.startswith("routing/") or module in ROUTING_MODULES:
        return "routing"
    if module.startswith("phases/") or module in PLACEMENT_MODULES:
        return "placement"
    return None


def _own_nodes(node: ast.AST) -> Iterator[ast.AST]:
    """Nodes of *node*'s own body, not descending into nested scopes."""
    stack = list(ast.iter_child_nodes(node))
    while stack:
        child = stack.pop()
        yield child
        if not isinstance(child, _SCOPES):
            stack.extend(ast.iter_child_nodes(child))


def _call_name(call: ast.Call) -> str | None:
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return None


def _names_in(node: ast.AST) -> set[str]:
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}


def _stored_names(node: ast.AST) -> set[str]:
    return {
        n.id
        for n in ast.walk(node)
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)
    }


class _CopyAliases(ast.NodeTransformer):
    """Rename aliases of the ``copy`` module's functions to their own names."""

    def __init__(self, aliases: dict[str, str]) -> None:
        self.aliases = aliases

    def visit_Name(self, node: ast.Name) -> ast.AST:
        return ast.copy_location(
            ast.Name(self.aliases.get(node.id, node.id), node.ctx), node
        )


def _parse(text: str) -> ast.Module:
    """Parse *text*, naming ``deepcopy``/``copy`` and ``copy`` module aliases
    canonically so a renamed import is read like the plain one."""
    tree = ast.parse(text)
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "copy":
            for item in node.names:
                if item.asname:
                    aliases[item.asname] = item.name
        elif isinstance(node, ast.Import):
            for item in node.names:
                if item.name == "copy" and item.asname:
                    aliases[item.asname] = "copy"
    return _CopyAliases(aliases).visit(tree) if aliases else tree


def parse_layout(root: Path = LAYOUT_ROOT) -> dict[str, ast.Module]:
    """Parsed layout modules keyed by their path under *root*."""
    return {
        path.relative_to(root).as_posix(): _parse(path.read_text())
        for path in sorted(root.rglob("*.py"))
    }


def parse_sources(sources: dict[str, str]) -> dict[str, ast.Module]:
    return {module: _parse(text) for module, text in sources.items()}


def _nested_definitions(node: ast.AST) -> list[ast.AST]:
    """Function and class definitions directly nested in *node*, at any depth
    of statements but not inside another definition."""
    found: list[ast.AST] = []
    stack = list(ast.iter_child_nodes(node))
    while stack:
        child = stack.pop()
        if isinstance(child, (*_DEFS, ast.ClassDef)):
            found.append(child)
            continue
        stack.extend(ast.iter_child_nodes(child))
    return found


def collect_functions(trees: dict[str, ast.Module]) -> list[Func]:
    funcs: list[Func] = []

    def visit(module: str, body: Iterable[ast.AST], prefix: str) -> None:
        for node in body:
            if isinstance(node, _DEFS):
                qual = f"{prefix}{node.name}"
                a = node.args
                params = [x.arg for x in (*a.posonlyargs, *a.args, *a.kwonlyargs)]
                funcs.append(Func(module, qual, node, params))
                visit(module, _nested_definitions(node), f"{qual}.")
            elif isinstance(node, ast.ClassDef):
                visit(module, node.body, f"{prefix}{node.name}.")

    for module, tree in trees.items():
        visit(module, tree.body, "")
    return funcs


def is_checker(func: Func) -> bool:
    return func.module in CHECKER_MODULES or func.name.startswith(CHECKER_PREFIXES)


def _calls(func: Func) -> list[ast.Call]:
    return [n for n in _own_nodes(func.node) if isinstance(n, ast.Call)]


def _first_arg_name(call: ast.Call) -> str | None:
    if call.args and isinstance(call.args[0], ast.Name):
        return call.args[0].id
    return None


def _is_non_writer(name: str) -> bool:
    return (
        name in NON_WRITER_NAMES
        or name.startswith(NON_WRITER_PREFIXES)
        or "guard" in name
    )


def _local_names(func: Func) -> set[str]:
    return {*func.params, *_stored_names(func.node)}


def _writes_ctx_offsets(func: Func) -> bool:
    if not func.params or func.params[0] != "ctx":
        return False
    for node in _own_nodes(func.node):
        if isinstance(node, ast.Assign | ast.AugAssign | ast.AnnAssign):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(
                isinstance(t, ast.Subscript) and ast.unparse(t.value) == "ctx.offsets"
                for t in targets
            ):
                return True
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in MUTATORS
            and ast.unparse(node.func.value) == "ctx.offsets"
        ):
            return True
    return False


def _table_statement_calls(func: Func) -> Iterator[ast.Call]:
    for stmt in _own_nodes(func.node):
        if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call):
            yield stmt.value


def _postpass_calls(func: Func) -> list[ast.Call]:
    """Calls in a routing table that take the emitted ``routes`` list and are
    not assertions, validations or snapshots."""
    return [
        call
        for call in _calls(func)
        if isinstance(call.func, ast.Name)
        and _first_arg_name(call) == "routes"
        and not call.func.id.startswith(POSTPASS_SKIP_PREFIXES)
    ]


def build_registry(
    funcs: list[Func],
    pinned_passes: Iterable[str] = (),
    pinned_nested_passes: Iterable[str] = (),
) -> Registry:
    reg = Registry()
    defined = {f.name for f in funcs}

    for func in funcs:
        callees = [_call_name(c) for c in _calls(func)]
        if func.module == ENGINE and callees.count("_snap") >= MIN_SNAPS_FOR_TABLE:
            reg.tables.add(func.name)
        if (
            func.module == OFFSETS
            and "_build_offset_ctx" in callees
            and "_hold_decided_orders" in callees
        ):
            reg.tables.add(func.name)

    for func in funcs:
        if func.name not in reg.tables:
            continue
        local = _local_names(func)
        for call in _table_statement_calls(func):
            name = _call_name(call)
            if name is None or _is_non_writer(name):
                continue
            if _first_arg_name(call) in {"graph", "ctx"} and name in defined:
                reg.table_phases.add(name)
            for arg in call.args:
                if (
                    isinstance(arg, ast.Name)
                    and arg.id in defined
                    and arg.id not in local
                    and not _is_non_writer(arg.id)
                ):
                    reg.table_phases.add(arg.id)
    reg.table_phases -= reg.tables

    for func in funcs:
        if func.module == SOLVER and func.name.startswith(SOLVER_PREFIXES):
            (reg.solver_local if "." in func.qualname else reg.solver).add(func.name)

    ctx_funcs = [f for f in funcs if f.module == OFFSETS and not is_checker(f)]
    reg.offset_writers = {f.name for f in ctx_funcs if _writes_ctx_offsets(f)}
    changed = True
    while changed:
        changed = False
        for func in ctx_funcs:
            if func.name in reg.offset_writers or func.params[:1] != ["ctx"]:
                continue
            if any(
                _call_name(c) in reg.offset_writers and _first_arg_name(c) == "ctx"
                for c in _calls(func)
            ):
                reg.offset_writers.add(func.name)
                changed = True
    reg.offset_writers -= reg.tables

    for module in (ENGINE, OFFSETS):
        scope = [f for f in funcs if f.module == module]
        local_tables = {f.name for f in scope if f.name in reg.tables}
        local_runners: set[str] = set()
        changed = True
        while changed:
            changed = False
            for func in scope:
                if func.name in local_runners or func.name in local_tables:
                    continue
                if any(
                    _call_name(c) in local_tables | local_runners for c in _calls(func)
                ):
                    local_runners.add(func.name)
                    changed = True
        reg.runners |= local_runners

    for func in funcs:
        snapshot_checks = [
            c for c in _calls(func) if _call_name(c) == "assert_exit_turn_snapshot"
        ]
        if (
            func.module == ROUTING
            and len(snapshot_checks) >= MIN_SNAPSHOT_CHECKS_FOR_ROUTING_TABLE
        ):
            reg.routing_tables.add(func.name)
    reg.later_passes = {
        f.name
        for f in funcs
        if f.name in reg.routing_tables
        or (
            f.module == ROUTING
            and any(_call_name(c) in reg.routing_tables for c in _calls(f))
        )
        or (f.module == OFFSETS and f.name in reg.runners)
    }
    reg.table_postpasses = {
        _call_name(call) or ""
        for f in funcs
        if f.name in reg.routing_tables
        for call in _postpass_calls(f)
    }
    reg.postpasses = reg.table_postpasses | set(pinned_passes)
    reg.nested_passes = set(pinned_nested_passes)
    reg.entry_passes = {f.name for f in funcs if f.name in reg.later_passes}
    reg.later_passes |= reg.postpasses | reg.nested_passes
    reg.runners -= reg.later_passes

    _find_delegates(funcs, reg)
    reg.wrappers = {
        f.name
        for f in funcs
        if f.name in reg.table_phases and reg.delegates.get(f.site)
    }
    return reg


def _registered_value(node: ast.AST, names: set[str]) -> str | None:
    if isinstance(node, ast.Name) and node.id in names:
        return node.id
    if (
        isinstance(node, ast.Call)
        and _call_name(node) == "partial"
        and node.args
        and isinstance(node.args[0], ast.Name)
        and node.args[0].id in names
    ):
        return node.args[0].id
    return None


def _find_delegates(funcs: list[Func], reg: Registry) -> None:
    by_name: dict[str, list[Func]] = {}
    for func in funcs:
        by_name.setdefault(func.name, []).append(func)
    names = reg.names - reg.solver_local
    for func in funcs:
        for call in _calls(func):
            for callee in by_name.get(_call_name(call) or "", ()):
                for param, arg in zip(callee.params, call.args, strict=False):
                    if _registered_value(arg, names):
                        reg.delegates.setdefault(callee.site, set()).add(param)
                for kw in call.keywords:
                    if kw.arg in callee.params and _registered_value(kw.value, names):
                        reg.delegates.setdefault(callee.site, set()).add(kw.arg)


def _is_registered(func: Func, name: str | None, reg: Registry) -> bool:
    if name is None:
        return False
    if name in reg.solver_local:
        return func.module == SOLVER
    return name in reg.names or name in reg.delegates.get(func.site, set())


def _copy_kind(node: ast.AST) -> str | None:
    """The kind of scratch copy *node* itself makes, or None."""
    if not isinstance(node, ast.Call):
        return None
    name = _call_name(node)
    if name == "deepcopy":
        return "deepcopy"
    if name == "copy" and (
        (isinstance(node.func, ast.Name))
        or (
            isinstance(node.func, ast.Attribute)
            and ast.unparse(node.func.value) == "copy"
        )
    ):
        return "copy"
    if (
        name == "copy"
        and isinstance(node.func, ast.Attribute)
        and any(t in ast.unparse(node.func.value) for t in COPY_RECEIVER_TERMS)
    ):
        return "copy"
    if name == "replace" and any(kw.arg in OFFSET_STATE_KWARGS for kw in node.keywords):
        return "replace"
    if name in COPY_WRAPPERS and "offsets" in ast.unparse(node):
        return name
    return None


def _scratch_in(node: ast.AST) -> str | None:
    for sub in ast.walk(node):
        kind = _copy_kind(sub)
        if kind:
            return kind
    return None


def _scratch_names(func: Func) -> dict[str, str]:
    """Names bound in *func*'s own body to a scratch copy, with the copy kind.
    A name rebuilt from a scratch name by another copy keeps its kind."""
    scratch: dict[str, str] = {}
    assigns = [n for n in _own_nodes(func.node) if isinstance(n, ast.Assign)]
    changed = True
    while changed:
        changed = False
        for node in assigns:
            kind = _scratch_in(node.value)
            if kind is None and _names_in(node.value) & set(scratch):
                inner = node.value
                if isinstance(inner, ast.Call) and (
                    _call_name(inner) in {"replace", "deepcopy", "copy", *COPY_WRAPPERS}
                ):
                    kind = scratch[sorted(_names_in(inner) & set(scratch))[0]]
            if kind is None:
                continue
            for name in {n for t in node.targets for n in _names_in(t)}:
                if name not in scratch:
                    scratch[name] = kind
                    changed = True
    return scratch


def _arg_values(call: ast.Call) -> list[ast.AST]:
    return [*call.args, *(kw.value for kw in call.keywords)]


def _scratch_kind_of(arg: ast.AST, scratch: dict[str, str]) -> str | None:
    kind = _scratch_in(arg)
    if kind is not None:
        return kind
    used = _names_in(arg) & set(scratch)
    return scratch[sorted(used)[0]] if used else None


def _receiver_or_first(call: ast.Call) -> ast.AST | None:
    if call.args:
        return call.args[0]
    if isinstance(call.func, ast.Attribute):
        return call.func.value
    return None


def _loop_body_nodes(loop: ast.stmt) -> Iterator[ast.AST]:
    stack: list[ast.AST] = list(ast.iter_child_nodes(loop))
    while stack:
        node = stack.pop()
        yield node
        if not isinstance(node, (*_DEFS, ast.Lambda)):
            stack.extend(ast.iter_child_nodes(node))


def _for_taint(loop: ast.For | ast.AsyncFor) -> set[str]:
    tainted = _names_in(loop.target)
    changed = True
    while changed:
        changed = False
        for node in _loop_body_nodes(loop):
            if isinstance(node, ast.Assign) and _names_in(node.value) & tainted:
                new = {n for t in node.targets for n in _names_in(t)} - tainted
            elif isinstance(node, ast.For | ast.AsyncFor) and (
                _names_in(node.iter) & tainted
            ):
                new = _names_in(node.target) - tainted
            else:
                continue
            if new:
                tainted |= new
                changed = True
    return tainted


def _while_taint(loop: ast.While) -> set[str]:
    return {n for node in _loop_body_nodes(loop) for n in _stored_names(node)}


def _with_header_names(loop: ast.stmt, call: ast.Call) -> set[str]:
    names: set[str] = set()
    for node in _loop_body_nodes(loop):
        if isinstance(node, ast.With | ast.AsyncWith) and any(
            sub is call for stmt in node.body for sub in ast.walk(stmt)
        ):
            for item in node.items:
                names |= _names_in(item.context_expr)
    return names


def _loop_exits(loop: ast.stmt) -> bool:
    """Whether *loop* can end early: a ``return`` anywhere in it, or a ``break``
    that is not inside an inner loop."""
    stack: list[tuple[ast.AST, bool]] = [
        (child, False) for child in ast.iter_child_nodes(loop)
    ]
    while stack:
        node, in_inner = stack.pop()
        if isinstance(node, ast.Return) or (
            isinstance(node, ast.Break) and not in_inner
        ):
            return True
        if isinstance(node, (*_DEFS, ast.Lambda)):
            continue
        inner = in_inner or isinstance(node, _LOOPS)
        stack.extend((child, inner) for child in ast.iter_child_nodes(node))
    return False


def _is_lazy_cache(loop: ast.stmt, call: ast.Call) -> bool:
    """Whether *call* fills a name once under ``if name is None`` / ``if not name``."""
    for node in _loop_body_nodes(loop):
        if not isinstance(node, ast.If):
            continue
        test = node.test
        guarded = None
        if (
            isinstance(test, ast.Compare)
            and isinstance(test.left, ast.Name)
            and len(test.ops) == 1
            and isinstance(test.ops[0], ast.Is)
            and isinstance(test.comparators[0], ast.Constant)
            and test.comparators[0].value is None
        ):
            guarded = test.left.id
        elif (
            isinstance(test, ast.UnaryOp)
            and isinstance(test.op, ast.Not)
            and isinstance(test.operand, ast.Name)
        ):
            guarded = test.operand.id
        if guarded is None:
            continue
        for stmt in node.body:
            for sub in ast.walk(stmt):
                if (
                    isinstance(sub, ast.Assign)
                    and any(inner is call for inner in ast.walk(sub.value))
                    and guarded in {n for t in sub.targets for n in _names_in(t)}
                ):
                    return True
    return False


def _iterates_literal_alternatives(loop: ast.stmt) -> bool:
    """Whether *loop* is a ``for`` over a literal tuple or list of two or more
    alternatives."""
    return (
        isinstance(loop, ast.For | ast.AsyncFor)
        and isinstance(loop.iter, ast.Tuple | ast.List)
        and len(loop.iter.elts) >= 2
    )


def _result_consumed(func: Func, call: ast.Call) -> bool:
    for node in _own_nodes(func.node):
        if isinstance(node, ast.Expr) and node.value is call:
            return False
    return True


Path_ = tuple[tuple[int, str, bool], ...]


def _paths(func: Func) -> dict[int, Path_]:
    """Per node, the ``(if-id, arm, arm-terminates)`` triples enclosing it."""
    out: dict[int, Path_] = {}

    def visit(node: ast.AST, path: Path_) -> None:
        out[id(node)] = path
        if isinstance(node, ast.If):
            visit(node.test, path)
            for arm, body in (("body", node.body), ("orelse", node.orelse)):
                ends = bool(body) and isinstance(body[-1], _TERMINATORS)
                for stmt in body:
                    visit(stmt, (*path, (id(node), arm, ends)))
            return
        for child in ast.iter_child_nodes(node):
            if not isinstance(child, (*_DEFS, ast.Lambda, ast.ClassDef)):
                visit(child, path)

    for child in ast.iter_child_nodes(func.node):
        if not isinstance(child, (*_DEFS, ast.Lambda, ast.ClassDef)):
            visit(child, ())
    return out


def _returned_calls(func: Func) -> set[int]:
    return {
        id(sub)
        for node in _own_nodes(func.node)
        if isinstance(node, ast.Return) and node.value is not None
        for sub in ast.walk(node.value)
        if isinstance(sub, ast.Call)
    }


def _exclusive(
    first: ast.Call, later: ast.Call, paths: dict[int, Path_], returned: set[int]
) -> bool:
    """Whether *later* cannot run after *first* in one invocation."""
    if id(first) in returned:
        return True
    path_first, path_later = paths.get(id(first), ()), paths.get(id(later), ())
    arms_later = {(i, arm) for i, arm, _ in path_later}
    ifs_later = {i for i, _, _ in path_later}
    for if_id, arm, ends in path_first:
        if if_id in ifs_later and (if_id, arm) not in arms_later:
            return True
        if ends and (if_id, arm) not in arms_later:
            return True
    return False


def _non_exclusive_pair(
    sites: list[ast.Call], paths: dict[int, Path_], returned: set[int]
) -> ast.Call | None:
    """The later site of the first pair of *sites* that can both run."""
    ordered = sorted(sites, key=lambda c: (c.lineno, c.col_offset))
    for i, first in enumerate(ordered):
        for later in ordered[i + 1 :]:
            if not _exclusive(first, later, paths, returned):
                return later
    return None


def _marker_in(name: str, markers: tuple[str, ...]) -> bool:
    low = name.lower()
    return any(m in low for m in markers)


class _Scanner:
    def __init__(self, funcs: list[Func], reg: Registry) -> None:
        self.funcs = funcs
        self.reg = reg
        self.hits: dict[tuple[str, str, str], Hit] = {}

    def add(
        self, rule: str, func: Func, callee: str, line: int, detail: str = ""
    ) -> None:
        hit = Hit(rule, func.site, callee, line, detail)
        known = self.hits.get(hit.key)
        if known is None or line < known.line:
            self.hits[hit.key] = hit

    def run(self) -> list[Hit]:
        for func in self.funcs:
            if is_checker(func):
                continue
            calls = _calls(func)
            if _marker_in(func.name, RETRY_MARKERS):
                self.add("named-retry", func, func.name, func.node.lineno)
            self.scratch_copies(func, calls)
            self.retry_loops(func)
            self.reruns(func, calls)
            self.later_pass_reads(func, calls)
            self.table_repairs(func)
            self.route_postpasses(func, calls)
        self.lookahead_callers()
        return sorted(self.hits.values(), key=lambda h: (h.site, h.line, h.rule))

    def scratch_copies(self, func: Func, calls: list[ast.Call]) -> None:
        scratch = _scratch_names(func)
        ran_on_copy = False
        for call in calls:
            name = _call_name(call)
            if name is None or name in COPY_CALLS | COPY_WRAPPERS | {"replace"}:
                continue
            first = _receiver_or_first(call)
            kind = _scratch_kind_of(first, scratch) if first is not None else None
            if kind is not None and _is_registered(func, name, self.reg):
                self.add("lookahead-copy", func, name, call.lineno, kind)
                ran_on_copy = True
        if ran_on_copy:
            return
        for call in calls:
            name = _call_name(call)
            if name is None or name in BUILTIN_CALLS | {"replace"}:
                continue
            if any(
                _scratch_kind_of(a, scratch) == "replace" for a in _arg_values(call)
            ):
                self.add("scratch-offsets", func, "<scratch context>", call.lineno)
                return

    def retry_loops(self, func: Func) -> None:
        for loop in (n for n in _own_nodes(func.node) if isinstance(n, _LOOPS)):
            tainted = (
                _while_taint(loop) if isinstance(loop, ast.While) else _for_taint(loop)  # type: ignore[arg-type]
            )
            returned = _returned_calls(func)
            for call in (c for c in _loop_body_nodes(loop) if isinstance(c, ast.Call)):
                name = _call_name(call)
                if name is None or not _is_registered(func, name, self.reg):
                    continue
                used = set().union(*(_names_in(a) for a in _arg_values(call)))
                if isinstance(call.func, ast.Attribute):
                    used |= _names_in(call.func.value)
                used |= _with_header_names(loop, call)
                returned_once = (
                    id(call) in returned
                    and not used & tainted
                    and not _iterates_literal_alternatives(loop)
                )
                if returned_once or _is_lazy_cache(loop, call):
                    continue
                if not used & tainted:
                    detail = "inputs independent of loop"
                elif _loop_exits(loop) and _result_consumed(func, call):
                    detail = "loop exits on result"
                else:
                    continue
                self.add("retry-loop", func, name, call.lineno, detail)

    def reruns(self, func: Func, calls: list[ast.Call]) -> None:
        reg = self.reg
        in_table = func.name in reg.tables
        sites_by_name: dict[str, list[ast.Call]] = {}
        for call in calls:
            name = _call_name(call)
            if name is None or not _is_registered(func, name, reg):
                continue
            if in_table and name in reg.wrappers:
                for arg in call.args:
                    if isinstance(arg, ast.Name) and arg.id in reg.table_phases:
                        sites_by_name.setdefault(arg.id, []).append(call)
                continue
            sites_by_name.setdefault(name, []).append(call)
        table_kinds = reg.table_phases | reg.offset_writers
        runner_kinds = reg.runners | reg.solver | reg.solver_local | reg.later_passes
        delegate_params = reg.delegates.get(func.site, set())
        paths = _paths(func)
        returned = _returned_calls(func)
        for name, sites in sites_by_name.items():
            if len(sites) < 2:
                continue
            if in_table:
                counted = name in table_kinds
            else:
                counted = name in runner_kinds or name in delegate_params
            later = _non_exclusive_pair(sites, paths, returned) if counted else None
            if later is not None:
                self.add("rerun", func, name, later.lineno)

    def later_pass_reads(self, func: Func, calls: list[ast.Call]) -> None:
        reg = self.reg
        if func.name in reg.tables | reg.runners | reg.routing_tables:
            return
        if func.name in reg.entry_passes:
            reportable = reg.postpasses | reg.nested_passes
        elif func.name in reg.postpasses | reg.nested_passes:
            reportable = reg.postpasses
        else:
            reportable = reg.later_passes
        for call in calls:
            name = _call_name(call)
            if name in reportable:
                self.add("later-pass-read", func, name or "", call.lineno)

    def table_repairs(self, func: Func) -> None:
        if func.name not in self.reg.tables:
            return
        for call in _table_statement_calls(func):
            name = _call_name(call)
            if name in self.reg.table_phases and _marker_in(name, REPAIR_MARKERS):
                self.add("repair-named", func, name, call.lineno)

    def route_postpasses(self, func: Func, calls: list[ast.Call]) -> None:
        if func.name not in self.reg.routing_tables:
            return
        for call in _postpass_calls(func):
            self.add("route-postpass", func, _call_name(call) or "", call.lineno)

    def lookahead_callers(self) -> None:
        """Flag every function that calls one that runs a copy.

        The chain stops at a registered phase, which is flagged and not followed.
        """
        reg = self.reg
        runs_copy = {h.site for h in self.hits.values() if h.rule == "lookahead-copy"}
        tainted = {site.split(":", 1)[1].rsplit(".", 1)[-1] for site in runs_copy}
        stops = (
            reg.table_phases
            | reg.offset_writers
            | reg.tables
            | reg.runners
            | reg.later_passes
        )
        changed = True
        while changed:
            changed = False
            for func in self.funcs:
                if is_checker(func) or func.site in runs_copy:
                    continue
                reached = {
                    c: _call_name(c)
                    for c in _calls(func)
                    if _call_name(c) in tainted - {func.name}
                }
                if not reached:
                    continue
                call, callee = min(reached.items(), key=lambda kv: kv[0].lineno)
                hit = Hit("calls-lookahead", func.site, callee or "", call.lineno)
                if hit.key in self.hits:
                    continue
                self.hits[hit.key] = hit
                if func.name not in stops | tainted:
                    tainted.add(func.name)
                    changed = True


def scan(
    trees: dict[str, ast.Module],
    pinned_passes: Iterable[str] = (),
    pinned_nested_passes: Iterable[str] = (),
) -> tuple[Registry, list[Hit]]:
    """The registry and every hit in *trees*, ordered by site and line.

    *pinned_passes* and *pinned_nested_passes* are registered as later passes
    whether or not a routing table calls them."""
    funcs = collect_functions(trees)
    reg = build_registry(funcs, pinned_passes, pinned_nested_passes)
    return reg, _Scanner(funcs, reg).run()
