"""Structural guard: no iteration or look-ahead re-enters ``src/nf_metro/layout/``.

The layout decides each coordinate once, from plan-time inputs, and later phases
consume the decision.  ``tests/layout_iteration_guard.py`` reads the layout
source with :mod:`ast` and reports code that runs a phase or writer on a copy,
runs a pass again, retries until a result holds, or rewrites a decision after
it was made.  Its docstring defines the registry and each detection rule.

The allowlist in ``tests/data/layout_iteration_allowlist.json`` names every
existing hit, the #2094 line that removes it and the plan-time rule that
replaces it.  It is a merge gate: it must be empty before PR #2015 merges to
``main``.  It may only shrink, which the tests enforce three ways: a hit with no
entry fails, an entry that matches no hit fails (so a removal must delete its
entry), and the entry count may not exceed ``ALLOWLIST_CEILING``.  Lower the
ceiling to the new count in the commit that removes entries.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import layout_iteration_guard as guard
import pytest

ALLOWLIST_PATH = Path(__file__).parent / "data" / "layout_iteration_allowlist.json"
ALLOWLIST_CEILING = 89
REMOVED_BY = re.compile(r"^(unassigned|\d+[a-z]?\b|R3\b)")


def _allowlist() -> list[dict[str, str]]:
    return json.loads(ALLOWLIST_PATH.read_text())["entries"]


def _where(hit: guard.Hit) -> str:
    return f"{hit.rule} {hit.site}:{hit.line} -> {hit.callee} {hit.detail}".rstrip()


@pytest.fixture(scope="module")
def live() -> tuple[guard.Registry, list[guard.Hit]]:
    return guard.scan(guard.parse_layout())


def test_layout_has_no_unlisted_iteration(live):
    _, hits = live
    allowed = {(e["rule"], e["site"], e["callee"]) for e in _allowlist()}
    new = [h for h in hits if h.key not in allowed]
    assert not new, (
        "iteration or look-ahead in the layout code; decide it at plan time "
        "instead of adding an allowlist entry:\n" + "\n".join(map(_where, new))
    )


def test_allowlist_has_no_stale_entries(live):
    _, hits = live
    found = {h.key for h in hits}
    stale = [
        f"{e['rule']} {e['site']} -> {e['callee']}"
        for e in _allowlist()
        if (e["rule"], e["site"], e["callee"]) not in found
    ]
    assert not stale, (
        "allowlist entries that no longer match a hit; delete them and lower "
        "ALLOWLIST_CEILING:\n" + "\n".join(stale)
    )


def test_allowlist_is_well_formed():
    entries = _allowlist()
    keys = [(e["rule"], e["site"], e["callee"]) for e in entries]
    assert len(keys) == len(set(keys)), "duplicate allowlist entries"
    for entry in entries:
        assert set(entry) == {"rule", "site", "callee", "removed_by", "replacement"}
        assert entry["rule"] in guard.RULES, entry
        assert REMOVED_BY.match(entry["removed_by"]), entry
        assert entry["replacement"].strip(), entry
    assert json.loads(ALLOWLIST_PATH.read_text())["gate"]


def test_allowlist_only_shrinks():
    assert len(_allowlist()) <= ALLOWLIST_CEILING


def test_registry_is_derived_from_the_source(live):
    """A rename that empties the registry must fail here, not pass silently."""
    reg, _ = live
    assert {"_compute_section_layout", "_run_offset_phases", "_route_edges"} <= (
        reg.tables | reg.routing_tables
    )
    assert len(reg.table_phases) > 50
    assert len(reg.offset_writers) > 20
    assert {"compute_layout", "_layout_once", "_compute_layout_scaled"} <= reg.runners
    assert {"compute_station_offsets", "route_edges"} <= reg.later_passes
    assert {"_solve_in_frame", "solve_corridor_cohorts"} <= reg.solver
    assert "seat" in reg.solver_local
    assert "_reconcile_horizontal_offsets" in reg.table_phases


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


def _hits(rule: str, **modules: str) -> list[guard.Hit]:
    sources = {name.replace("__", "/") + ".py": text for name, text in modules.items()}
    sources.setdefault("engine.py", ENGINE)
    sources.setdefault("routing/offsets.py", OFFSETS)
    _, hits = guard.scan(guard.parse_sources(sources))
    return [h for h in hits if h.rule == rule]


def _callees(rule: str, **modules: str) -> set[tuple[str, str]]:
    return {(h.site.split(":", 1)[1], h.callee) for h in _hits(rule, **modules)}


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
