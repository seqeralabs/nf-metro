"""Structural guard: no iteration or look-ahead re-enters ``src/nf_metro/layout/``.

The layout decides each coordinate once, from plan-time inputs, and later phases
consume the decision.  ``tests/layout_iteration_guard.py`` reads the layout
source with :mod:`ast` and reports code that runs a phase or writer on a copy,
runs a pass again, retries until a result holds, or rewrites a decision after
it was made.  Its docstring defines the registry and each detection rule.

The allowlist in ``tests/data/layout_iteration_allowlist.json`` names every
existing hit, who removes it and the plan-time rule that replaces it.  It has two
lists, each checked on its own:

* ``routing`` (route planning and emission: ``layout/routing/`` and the planning
  modules named by ``guard.ROUTING_MODULES``) is the PR #2015 merge gate: it
  must be empty before #2015 merges to ``main``.  Its ``manual`` section holds
  cases the AST cannot see; each is checked only by asserting that the named
  function exists, so removing the function makes the entry stale.  Its pins
  (``pinned_later_passes``, ``pinned_nested_passes``) keep route passes
  registered while anything calls them.
* ``placement`` (``engine.py``, ``phases/``, ``geometric_bypass.py`` and the other
  modules of ``guard.PLACEMENT_MODULES``) is not a #2015 gate.  It is shrink-only
  and owned by #2171, the placement programme (#323/#1745).

Each list may only shrink, which the tests enforce three ways: a hit with no
entry fails, an entry that matches no hit fails (so a removal must delete its
entry), and the entry count must equal the list's ceiling, so the commit that
removes entries lowers the ceiling and a later addition has to raise it.  A commit
that deletes one entry and adds another leaves the count unchanged; only the
allowlist diff shows it.  Every layout module has a role, so
a new module forces a decision about which list its hits belong to.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import layout_iteration_guard as guard
import pytest

ALLOWLIST_PATH = Path(__file__).parent / "data" / "layout_iteration_allowlist.json"
ROUTING_CEILING = 72
PLACEMENT_CEILING = 44
PINNED_LATER_CEILING = 22
PINNED_NESTED_CEILING = 1
LISTS = ("routing", "placement")
PLACEMENT_OWNER = "#2171"
ROUTING_REMOVED_BY = re.compile(r"^(unassigned|\d+[a-z]?\b|R3\b|LANE-PLAN\b)")
OPTIONAL_ENTRY_FIELDS = {"note"}
MANUAL_FIELDS = {"site", "shape", "removed_by", "replacement"}
ENTRY_FIELDS = {"rule", "site", "callee", "removed_by", "replacement"}


def _data() -> dict[str, dict]:
    return json.loads(ALLOWLIST_PATH.read_text())


def _entries(name: str) -> list[dict[str, str]]:
    return _data()[name]["entries"]


def _manual() -> list[dict[str, str]]:
    return _data()["routing"]["manual"]


def _pins(kind: str) -> list[str]:
    key = "pinned_nested_passes" if kind == "nested" else "pinned_later_passes"
    return _data()["routing"][key]


def _key(entry: dict[str, str]) -> tuple[str, str, str]:
    return (entry["rule"], entry["site"], entry["callee"])


def _in_list(name: str, site: str) -> bool:
    return guard.module_role(site.split(":", 1)[0]) == name


def _where(hit: guard.Hit) -> str:
    return f"{hit.rule} {hit.site}:{hit.line} -> {hit.callee} {hit.detail}".rstrip()


@pytest.fixture(scope="module")
def live() -> tuple[guard.Registry, list[guard.Hit]]:
    return guard.scan(guard.parse_layout(), _pins("routing"), _pins("nested"))


@pytest.mark.parametrize("name", LISTS)
def test_layout_has_no_unlisted_iteration(live, name):
    _, hits = live
    allowed = {_key(e) for e in _entries(name)}
    new = [h for h in hits if _in_list(name, h.site) and h.key not in allowed]
    assert not new, (
        f"iteration or look-ahead in the layout code ({name}); decide it at plan "
        "time instead of adding an allowlist entry:\n" + "\n".join(map(_where, new))
    )


@pytest.mark.parametrize("name", LISTS)
def test_allowlist_has_no_stale_entries(live, name):
    _, hits = live
    found = {h.key for h in hits}
    stale = [" ".join(_key(e)) for e in _entries(name) if _key(e) not in found]
    assert not stale, (
        f"{name} allowlist entries that match no hit; delete them and "
        "lower the ceiling:\n" + "\n".join(stale)
    )


def test_manual_entries_name_existing_functions():
    funcs = guard.collect_functions(guard.parse_layout())
    defined = {f.site for f in funcs}
    stale = [m["site"] for m in _manual() if m["site"] not in defined]
    assert not stale, (
        "manual entries whose function is gone; delete them and lower "
        "ROUTING_CEILING:\n" + "\n".join(stale)
    )


@pytest.mark.parametrize("name", LISTS)
def test_allowlist_is_well_formed(name):
    entries = _entries(name)
    keys = [_key(e) for e in entries]
    assert len(keys) == len(set(keys)), "duplicate allowlist entries"
    for entry in entries:
        assert ENTRY_FIELDS <= set(entry) <= ENTRY_FIELDS | OPTIONAL_ENTRY_FIELDS, entry
        assert entry["rule"] in guard.RULES, entry
        assert _in_list(name, entry["site"]), f"{entry['site']} is in the wrong list"
        assert entry["replacement"].strip(), entry
        if name == "routing":
            assert ROUTING_REMOVED_BY.match(entry["removed_by"]), entry
        else:
            assert entry["removed_by"] == PLACEMENT_OWNER, entry
    assert _data()[name]["gate"]


def test_manual_entries_are_well_formed():
    manual = _manual()
    assert len({m["site"] for m in manual}) == len(manual)
    for entry in manual:
        assert set(entry) == MANUAL_FIELDS, entry
        assert _in_list("routing", entry["site"]), entry
        assert ROUTING_REMOVED_BY.match(entry["removed_by"]), entry
        assert entry["replacement"].strip(), entry


def test_routing_allowlist_count_is_the_ceiling():
    assert len(_entries("routing")) + len(_manual()) == ROUTING_CEILING, (
        "set ROUTING_CEILING to the new count"
    )


def test_placement_allowlist_count_is_the_ceiling():
    assert len(_entries("placement")) == PLACEMENT_CEILING, (
        "set PLACEMENT_CEILING to the new count"
    )


def test_every_layout_module_has_a_role():
    unclassified = [m for m in guard.parse_layout() if guard.module_role(m) is None]
    assert not unclassified, (
        "add each module to guard.ROUTING_MODULES or guard.PLACEMENT_MODULES:\n"
        + "\n".join(unclassified)
    )


def test_registry_is_derived_from_the_source(live):
    """A rename that empties the registry must fail here, not pass silently.

    Pins entry points that no allowlist entry targets, plus count floors."""
    reg, _ = live
    assert {"_compute_section_layout", "_run_offset_phases"} <= reg.tables
    assert "_route_edges" in reg.routing_tables
    assert len(reg.table_phases) > 50
    assert len(reg.offset_writers) > 20
    assert "compute_layout" in reg.runners
    assert {
        "route_edges",
        "route_edges_centred",
        "observe_route_edges",
        "compute_station_offsets",
    } <= reg.later_passes
    assert "solve_corridor_cohorts" in reg.solver


def _call_sites(funcs: list[guard.Func], name: str) -> list[str]:
    return sorted(
        f.site
        for f in funcs
        if not guard.is_checker(f)
        and any(guard._call_name(c) == name for c in guard._calls(f))
    )


def test_every_routing_table_pass_is_pinned(live):
    reg, _ = live
    missing = sorted(reg.table_postpasses - set(_pins("later")))
    assert not missing, (
        "add each pass the routing table runs to pinned_later_passes:\n"
        + "\n".join(missing)
    )


def test_pinned_passes_are_called_somewhere(live):
    reg, _ = live
    funcs = guard.collect_functions(guard.parse_layout())
    defined = {f.name for f in funcs}
    problems = []
    for name in _pins("later") + _pins("nested"):
        sites = _call_sites(funcs, name)
        in_table = name in reg.table_postpasses
        if name not in defined:
            problems.append(f"{name} is not defined; delete its pin and its rows")
        elif not sites:
            problems.append(f"{name} has no callers; delete its pin")
        elif name in _pins("later") and not in_table:
            problems.append(
                f"{name} no longer runs in the routing table but is called at "
                f"{', '.join(sites)}; remove those calls, then its pin"
            )
    assert not problems, "\n".join(problems)


def test_a_sub_pass_runs_inside_one_pass(live):
    reg, _ = live
    funcs = guard.collect_functions(guard.parse_layout())
    passes = reg.postpasses | reg.nested_passes
    problems = []
    for pin in _pins("nested"):
        sites = sorted(
            f.site
            for f in funcs
            if f.name in passes
            and f.name != pin
            and any(guard._call_name(c) == pin for c in guard._calls(f))
        )
        if len(sites) > 1:
            problems.append(
                f"{pin} runs inside {', '.join(sites)}; a sub-pass runs inside one pass"
            )
    assert not problems, "\n".join(problems)


def test_pin_lists_are_sorted_and_unique():
    for kind in ("later", "nested"):
        pins = _pins(kind)
        assert pins == sorted(set(pins))
    assert not set(_pins("later")) & set(_pins("nested"))


def test_pin_counts_are_the_ceilings():
    assert len(_pins("later")) == PINNED_LATER_CEILING
    assert len(_pins("nested")) == PINNED_NESTED_CEILING


# ---------------------------------------------------------------------------
# Detector behaviour on synthetic sources.
# ---------------------------------------------------------------------------

ENGINE = """
def _compute_section_layout(graph):
    _snap(graph, "1")
    phase_a(graph)
    _snap(graph, "2")
    phase_b(graph)
    _snap(graph, "3")

def _layout_once(graph):
    _compute_section_layout(graph)

def phase_a(graph):
    graph.x = 1

def phase_b(graph):
    graph.y = 1
"""

OFFSETS = """
def _run_offset_phases(graph, resolved):
    ctx = _build_offset_ctx(graph, resolved)
    write_offsets(ctx)
    _hold_decided_orders(ctx, "x")
    return ctx

def write_offsets(ctx):
    ctx.offsets[("a", "b")] = 1.0

def compute_station_offsets(graph):
    return _run_offset_phases(graph, 1.0).offsets
"""


def _hits(
    rule: str,
    pinned: tuple[str, ...] = (),
    nested: tuple[str, ...] = (),
    **modules: str,
) -> list[guard.Hit]:
    sources = {name.replace("__", "/") + ".py": text for name, text in modules.items()}
    sources.setdefault("engine.py", ENGINE)
    sources.setdefault("routing/offsets.py", OFFSETS)
    _, hits = guard.scan(guard.parse_sources(sources), pinned, nested)
    return [h for h in hits if h.rule == rule]


def _callees(rule: str, **modules: str) -> set[tuple[str, str]]:
    return {(h.site.split(":", 1)[1], h.callee) for h in _hits(rule, **modules)}


CORE_TABLE = """
def _route_edges(graph):
    routes = []
    tidy_trunks(routes, graph)
    assert_exit_turn_snapshot(routes, 1, 'a')
    assert_exit_turn_snapshot(routes, 1, 'b')
    assert_exit_turn_snapshot(routes, 1, 'c')
"""
PLANNER = """
def plan(copies, graph):
    tidy_trunks(copies, graph)
"""


def test_registry_of_synthetic_sources():
    sources = {"engine.py": ENGINE, "routing/offsets.py": OFFSETS}
    funcs = guard.collect_functions(guard.parse_sources(sources))
    reg = guard.build_registry(funcs)
    assert reg.tables == {"_compute_section_layout", "_run_offset_phases"}
    assert reg.table_phases == {"phase_a", "phase_b", "write_offsets"}
    assert "_layout_once" in reg.runners
    assert "compute_station_offsets" in reg.later_passes


class TestLookaheadCopy:
    def test_phase_run_on_a_deepcopy_is_flagged(self):
        src = ENGINE + (
            "\ndef decide(graph):\n"
            "    trial = deepcopy(graph)\n"
            "    phase_a(trial)\n"
            "    return trial.x\n"
        )
        assert ("decide", "phase_a") in _callees("lookahead-copy", engine=src)

    def test_writer_run_on_a_replaced_context_is_flagged(self):
        src = OFFSETS + (
            "\ndef judge(ctx):\n"
            "    flat = replace(ctx, offsets=dict(ctx.offsets))\n"
            "    write_offsets(flat)\n"
            "    return flat.offsets\n"
        )
        got = _callees("lookahead-copy", routing__offsets=src)
        assert ("judge", "write_offsets") in got

    def test_inline_scratch_copy_is_flagged(self):
        src = OFFSETS + (
            "\ndef judge(ctx):\n"
            "    return write_offsets(replace(ctx, offsets=dict(ctx.offsets)))\n"
        )
        assert ("judge", "write_offsets") in _callees(
            "lookahead-copy", routing__offsets=src
        )

    def test_delegate_called_on_a_scratch_context_is_flagged(self):
        src = OFFSETS + (
            "\ndef commit(ctx, write):\n"
            "    trial = replace(ctx, offsets=dict(ctx.offsets))\n"
            "    write(trial)\n"
            "\ndef caller(ctx):\n"
            "    commit(ctx, write_offsets)\n"
        )
        assert ("commit", "write") in _callees("lookahead-copy", routing__offsets=src)

    def test_callers_of_a_lookahead_function_are_flagged(self):
        src = OFFSETS + (
            "\ndef judge(ctx):\n"
            "    flat = replace(ctx, offsets=dict(ctx.offsets))\n"
            "    write_offsets(flat)\n"
            "\ndef decide(ctx):\n"
            "    if judge(ctx):\n"
            "        return None\n"
        )
        assert ("decide", "judge") in _callees("calls-lookahead", routing__offsets=src)

    def test_before_snapshot_passed_as_a_later_argument_is_not_flagged(self):
        src = OFFSETS + (
            "\ndef claim(ctx, before):\n"
            "    ctx.offsets[('a', 'b')] = len(before)\n"
            "\ndef run(ctx):\n"
            "    before = dict(ctx.offsets)\n"
            "    write_offsets(ctx)\n"
            "    claim(ctx, before)\n"
        )
        assert not _hits("lookahead-copy", routing__offsets=src)

    def test_live_state_is_not_flagged(self):
        src = ENGINE + "\ndef plain(graph):\n    phase_a(graph)\n"
        assert not _hits("lookahead-copy", engine=src)

    def test_aliased_deepcopy_import_is_flagged(self):
        src = (
            "from copy import deepcopy as dc\n" + ENGINE + "\ndef decide(graph):\n"
            "    trial = dc(graph)\n"
            "    phase_a(trial)\n"
        )
        assert ("decide", "phase_a") in _callees("lookahead-copy", engine=src)

    def test_copy_module_alias_is_flagged(self):
        src = (
            "import copy as cp\n" + ENGINE + "\ndef decide(graph):\n"
            "    trial = cp.copy(graph)\n"
            "    phase_a(trial)\n"
        )
        assert ("decide", "phase_a") in _callees("lookahead-copy", engine=src)

    def test_method_copy_of_offsets_is_flagged(self):
        src = OFFSETS + (
            "\ndef judge(ctx):\n"
            "    scratch = ctx.offsets.copy()\n"
            "    write_offsets(scratch)\n"
        )
        assert ("judge", "write_offsets") in _callees(
            "lookahead-copy", routing__offsets=src
        )


class TestRetryLoop:
    def test_phase_in_a_loop_over_unrelated_values_is_flagged(self):
        src = ENGINE + (
            "\ndef settle(graph):\n"
            "    for _ in range(3):\n"
            "        phase_a(graph)\n"
            "        if done(graph):\n"
            "            break\n"
        )
        assert ("settle", "phase_a") in _callees("retry-loop", engine=src)

    def test_phase_in_a_while_loop_is_flagged(self):
        src = ENGINE + (
            "\ndef settle(graph):\n    while not done(graph):\n        phase_a(graph)\n"
        )
        assert ("settle", "phase_a") in _callees("retry-loop", engine=src)

    def test_runner_in_a_spread_loop_is_flagged(self):
        src = ENGINE + (
            "\ndef spread(graph, pitch):\n"
            "    for attempt in range(4):\n"
            "        _layout_once(graph)\n"
            "        pitch = pitch + 1\n"
        )
        assert ("spread", "_layout_once") in _callees("retry-loop", engine=src)

    def test_solver_over_alternative_orders_is_flagged(self):
        solver = (
            "def _solve_in_frame(problem, orders):\n"
            "    def seat(order):\n"
            "        return order\n"
            "    for order in orders:\n"
            "        result = seat(order)\n"
            "        if result.ok:\n"
            "            return result\n"
            "    return None\n"
        )
        got = _callees("retry-loop", routing__corridor_cohorts=solver)
        assert ("_solve_in_frame", "seat") in got

    def test_one_application_per_item_is_not_flagged(self):
        src = ENGINE + (
            "\ndef per_row(graph, rows):\n"
            "    for row in rows:\n"
            "        with scoped(graph, row):\n"
            "            phase_a(graph)\n"
            "    for section in graph.sections:\n"
            "        phase_b(section)\n"
        )
        assert not _hits("retry-loop", engine=src)

    def test_worklist_walk_is_not_flagged(self):
        src = OFFSETS + (
            "\ndef walk(ctx, start):\n"
            "    frontier = [start]\n"
            "    while frontier:\n"
            "        node = frontier.pop()\n"
            "        write_offsets_at(ctx, node)\n"
            "\ndef write_offsets_at(ctx, node):\n"
            "    ctx.offsets[(node, 'a')] = 0.0\n"
        )
        assert not _hits("retry-loop", routing__offsets=src)

    def test_lazy_cache_in_a_loop_is_not_a_retry(self):
        src = ENGINE + (
            "\ndef size(graph, sections):\n"
            "    offsets = None\n"
            "    for section in sections:\n"
            "        if offsets is None:\n"
            "            offsets = phase_a(graph)\n"
            "        use(section, offsets)\n"
        )
        assert not _hits("retry-loop", engine=src)

    def test_returned_call_in_a_loop_is_not_a_retry(self):
        src = ENGINE + (
            "\ndef first(graph, sections):\n"
            "    for section in sections:\n"
            "        if section.ok:\n"
            "            return phase_a(graph)\n"
        )
        assert not _hits("retry-loop", engine=src)

    def test_ladder_over_literal_alternatives_is_flagged(self):
        solver = (
            "def _solve_in_frame(problem, a, b, c):\n"
            "    def seat(order, forced):\n"
            "        return order\n"
            "    for pair_orders in (a, b, c):\n"
            "        forced = query(pair_orders)\n"
            "        if forced:\n"
            "            return seat(pair_orders, forced)\n"
            "    return None\n"
        )
        got = _callees("retry-loop", routing__corridor_cohorts=solver)
        assert ("_solve_in_frame", "seat") in got

    def test_ladder_over_a_named_tuple_is_flagged(self):
        solver = (
            "def _solve_in_frame(problem, a, b, c):\n"
            "    def seat(order, forced):\n"
            "        return order\n"
            "    orders = (a, b, c)\n"
            "    for pair_orders in orders:\n"
            "        forced = query(pair_orders)\n"
            "        if forced:\n"
            "            return seat(pair_orders, forced)\n"
            "    return None\n"
        )
        got = _callees("retry-loop", routing__corridor_cohorts=solver)
        assert ("_solve_in_frame", "seat") in got

    def test_loop_returning_the_first_frame_that_solves_is_flagged(self):
        solver = (
            "def _solve_in_either_frame(problem):\n"
            "    frames = (problem.own, problem.opposite)\n"
            "    for frame in frames:\n"
            "        if ok(frame):\n"
            "            return _solve_in_frame(problem, frame)\n"
            "def _solve_in_frame(problem, frame):\n"
            "    return frame\n"
        )
        got = _callees("retry-loop", routing__corridor_cohorts=solver)
        assert ("_solve_in_either_frame", "_solve_in_frame") in got

    def test_return_in_an_inner_loop_is_an_exit(self):
        src = ENGINE + (
            "\ndef search(graph, pitches, checks):\n"
            "    for pitch in pitches:\n"
            "        result = _layout_once(graph, pitch)\n"
            "        for check in checks:\n"
            "            if check(result):\n"
            "                return result\n"
        )
        assert ("search", "_layout_once") in _callees("retry-loop", engine=src)

    def test_assignment_in_the_else_arm_is_not_a_cache(self):
        src = ENGINE + (
            "\ndef size(graph, sections):\n"
            "    offsets = None\n"
            "    for section in sections:\n"
            "        if offsets is None:\n"
            "            use(section)\n"
            "        else:\n"
            "            offsets = phase_a(graph)\n"
        )
        assert ("size", "phase_a") in _callees("retry-loop", engine=src)

    def test_break_in_an_inner_loop_is_not_the_outer_exit(self):
        src = ENGINE + (
            "\ndef per(graph, rows):\n"
            "    for row in rows:\n"
            "        result = phase_a(row)\n"
            "        for cell in row:\n"
            "            if cell:\n"
            "                break\n"
        )
        assert not _hits("retry-loop", engine=src)


class TestRerun:
    def test_runner_called_from_two_sites_is_flagged(self):
        src = ENGINE + (
            "\ndef relay(graph, changed):\n"
            "    _layout_once(graph)\n"
            "    if changed:\n"
            "        _layout_once(graph)\n"
        )
        assert ("relay", "_layout_once") in _callees("rerun", engine=src)

    def test_exclusive_branches_are_not_flagged(self):
        src = ENGINE + (
            "\ndef relay(graph, flat):\n"
            "    if flat:\n"
            "        _layout_once(graph)\n"
            "    else:\n"
            "        _layout_once(graph)\n"
            "\ndef early(graph, flat):\n"
            "    if flat:\n"
            "        _layout_once(graph)\n"
            "        return\n"
            "    _layout_once(graph)\n"
            "\ndef returned(graph, flat):\n"
            "    if flat:\n"
            "        return _layout_once(graph)\n"
            "    return _layout_once(graph)\n"
        )
        assert not _hits("rerun", engine=src)

    def test_phase_repeated_in_a_table_is_flagged(self):
        engine = ENGINE.replace(
            '    _snap(graph, "3")\n',
            '    _snap(graph, "3")\n    phase_a(graph)\n',
        )
        assert ("_compute_section_layout", "phase_a") in _callees(
            "rerun", engine=engine
        )

    def test_wrapped_phase_counts_as_the_phase(self):
        engine = ENGINE.replace(
            "    phase_a(graph)\n",
            '    _run_placement(graph, True, "1", phase_a)\n',
        ).replace(
            "    phase_b(graph)\n",
            '    _run_placement(graph, True, "2", phase_b)\n',
        )
        engine += "\ndef _run_placement(graph, validate, stage, fn):\n    fn(graph)\n"
        assert not _hits("rerun", engine=engine)


class TestLaterPassAndRepair:
    def test_phase_reading_a_later_pass_is_flagged(self):
        src = ENGINE + (
            "\ndef size(graph):\n    return compute_station_offsets(graph)\n"
        )
        assert ("size", "compute_station_offsets") in _callees(
            "later-pass-read", engine=src
        )

    def test_postpass_run_by_a_planner_is_flagged(self):
        core = (
            "def _route_edges(graph):\n"
            "    routes = []\n"
            "    tidy_trunks(routes, graph)\n"
            "    assert_exit_turn_snapshot(routes, 1, 'a')\n"
            "    assert_exit_turn_snapshot(routes, 1, 'b')\n"
            "    assert_exit_turn_snapshot(routes, 1, 'c')\n"
        )
        planner = (
            "def seat_snapshot(copies, graph):\n"
            "    tidy_trunks(copies, graph)\n"
            "    tidy_trunks(copies, graph)\n"
            "    return copies\n"
            "def plan(routes, graph):\n"
            "    tidy_trunks(routes, graph)\n"
        )
        got = _callees(
            "later-pass-read", routing__core=core, routing__member_geometry=planner
        )
        assert got == {("seat_snapshot", "tidy_trunks"), ("plan", "tidy_trunks")}

    def test_pinned_pass_stays_registered_after_it_leaves_the_table(self):
        core = CORE_TABLE.replace("    tidy_trunks(routes, graph)\n", "")
        assert not _hits("later-pass-read", routing__core=core, routing__plan=PLANNER)
        got = _hits(
            "later-pass-read",
            pinned=("tidy_trunks",),
            routing__core=core,
            routing__plan=PLANNER,
        )
        assert {(h.site, h.callee) for h in got} == {
            ("routing/plan.py:plan", "tidy_trunks")
        }

    def test_table_pass_run_nested_in_another_pass_is_flagged(self):
        core = CORE_TABLE.replace(
            "    tidy_trunks(routes, graph)\n",
            "    tidy_trunks(routes, graph)\n    tidy_gaps(routes, graph)\n",
        )
        passes = (
            "def tidy_trunks(routes, graph):\n"
            "    tidy_gaps(routes, graph)\n"
            "    tidy_tails(routes, graph)\n"
            "def tidy_gaps(routes, graph):\n"
            "    return None\n"
        )
        got = _callees(
            "later-pass-read",
            routing__core=core,
            routing__normalize=passes,
        )
        assert got == {("tidy_trunks", "tidy_gaps")}

    def test_nested_pass_run_by_a_planner_is_flagged(self):
        got = _hits(
            "later-pass-read",
            nested=("tidy_tails",),
            routing__core=CORE_TABLE,
            routing__plan=PLANNER.replace("tidy_trunks", "tidy_tails"),
        )
        assert {(h.site, h.callee) for h in got} == {
            ("routing/plan.py:plan", "tidy_tails")
        }

    def test_repair_named_table_phase_is_flagged(self):
        engine = ENGINE.replace("phase_b", "restore_order")
        assert ("_compute_section_layout", "restore_order") in _callees(
            "repair-named", engine=engine
        )

    def test_named_retry_is_flagged(self):
        src = ENGINE + "\ndef compact_with_retry(ctx):\n    return ctx\n"
        assert ("compact_with_retry", "compact_with_retry") in _callees(
            "named-retry", engine=src
        )

    def test_route_postpass_in_a_routing_table_is_flagged(self):
        core = (
            "def _route_edges(graph):\n"
            "    routes = []\n"
            "    tidy_trunks(routes, graph)\n"
            "    assert_exit_turn_snapshot(routes, 1, 'a')\n"
            "    tidy_corners(routes, graph)\n"
            "    assert_exit_turn_snapshot(routes, 1, 'b')\n"
            "    assert_exit_turn_snapshot(routes, 1, 'c')\n"
            "    return routes\n"
            "\ndef route_edges(graph):\n"
            "    return _route_edges(graph)\n"
        )
        got = _callees("route-postpass", routing__core=core)
        assert got == {
            ("_route_edges", "tidy_trunks"),
            ("_route_edges", "tidy_corners"),
        }

    def test_scratch_context_given_to_a_planner_is_flagged(self):
        src = OFFSETS + (
            "\ndef plan(ctx, tentative):\n"
            "    return classify(replace(ctx, station_offsets=tentative))\n"
        )
        assert ("plan", "<scratch context>") in _callees(
            "scratch-offsets", routing__offsets=src
        )

    def test_guards_are_not_scanned(self):
        guards = (
            "def _guard_check(graph):\n"
            "    trial = deepcopy(graph)\n"
            "    phase_a(trial)\n"
            "    return compute_station_offsets(graph)\n"
        )
        for rule in guard.RULES:
            assert not _hits(rule, phases__guards=guards)
