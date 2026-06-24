"""Ratchet on routing gate-arm coverage (#677).

The routing subpackage dispatches each edge through priority-ordered handlers
and post-passes; every ``if``/``while`` is a *gate* whose two arms either are
or are not exercised by the fixture corpus.  A gate arm reached by no fixture
is an implicit assumption that only a future, never-seen topology will probe.

``scripts/routing_gate_coverage.py`` enumerates these gates and records, per
arm, which corpus fixtures reach it; ``docs/dev/routing_gate_coverage.md`` is
the published matrix and ``tests/data/routing_gate_coverage_baseline.json``
the frozen set of gates with an un-exercised arm.

The ratchet: a routing conditional may not have an un-exercised arm unless it
is listed in the baseline, and the baseline may not list a gate the corpus
fully exercises.  Adding a half-covered gate, or covering a baselined gate
without regenerating, fails this test.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
BASELINE_PATH = PROJECT_ROOT / "tests" / "data" / "routing_gate_coverage_baseline.json"
SCRIPT_PATH = PROJECT_ROOT / "scripts" / "routing_gate_coverage.py"


@pytest.fixture(scope="module")
def rgc():
    """The coverage script as an importable module, behind the version skip.

    The arc model is interpreter-specific, so the baseline only holds on the
    pinned CPython; skip elsewhere.
    """
    sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
    import routing_gate_coverage as module

    if sys.version_info[:2] != module.BASELINE_PYTHON:
        pytest.skip(
            f"gate baseline is pinned to CPython {module.BASELINE_PYTHON[0]}."
            f"{module.BASELINE_PYTHON[1]}; coverage's arc model differs on "
            f"{sys.version_info[0]}.{sys.version_info[1]}"
        )
    return module


@pytest.fixture(scope="module")
def gates(rgc):
    """Every routing gate with per-arm coverage, computed once per module.

    The sweep runs in a seed-pinned subprocess (``PYTHONHASHSEED`` fixed) rather
    than in-process: operand-level arc coverage is hash-seed sensitive because
    the layout engine iterates hash-ordered sets, so a fixed seed makes the gate
    set reproducible.  A fresh interpreter also sidesteps the can't-nest rule
    when the suite itself runs under a coverage tracer.
    """
    proc = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--json"],
        cwd=str(PROJECT_ROOT),
        env={**os.environ, "PYTHONHASHSEED": rgc.PINNED_HASH_SEED},
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, f"coverage sweep failed:\n{proc.stderr[-3000:]}"
    return rgc.gates_from_payload(json.loads(proc.stdout))


@pytest.fixture(scope="module")
def current_gaps(rgc, gates) -> set[str]:
    return set(rgc.gap_keys(gates))


@pytest.fixture(scope="module")
def baseline_gaps() -> set[str]:
    return set(json.loads(BASELINE_PATH.read_text()))


def test_no_new_un_exercised_routing_gate_arm(current_gaps, baseline_gaps):
    """No routing gate may have an un-exercised arm outside the baseline."""
    newly_uncovered = sorted(current_gaps - baseline_gaps)
    assert not newly_uncovered, (
        "Routing gate(s) gained an un-exercised arm relative to the baseline. "
        "Either author a fixture that hits both arms, or - if the new arm is "
        "genuinely unreachable - confirm it and regenerate the baseline with "
        "`python scripts/routing_gate_coverage.py --write`.\nNew gaps:\n  "
        + "\n  ".join(newly_uncovered)
    )


def test_gate_coverage_baseline_in_sync(current_gaps, baseline_gaps):
    """The committed baseline must not claim gaps the corpus exercises.

    A baseline listing a gate the corpus fully exercises silently weakens the
    ratchet, so closing a gap must be paired with regenerating the baseline.
    """
    now_covered = sorted(baseline_gaps - current_gaps)
    assert not now_covered, (
        "The corpus exercises both arms of gate(s) still listed in the "
        "baseline. Regenerate it with "
        "`python scripts/routing_gate_coverage.py --write` to tighten the "
        "ratchet.\nNewly covered:\n  " + "\n  ".join(now_covered)
    )


def test_triage_sidecar_references_open_gaps(rgc, gates):
    """Every triage verdict must name a gate that is an open gap.

    A verdict whose gate the corpus exercises (or whose key text has diverged)
    would silently mis-describe a closed gate, so it must be pruned.
    """
    stale = rgc.triage_stale_keys(gates, rgc.load_triage())
    assert not stale, (
        "Triage sidecar entr(y/ies) name gates that are not open gaps; remove "
        "them from tests/data/routing_gate_triage.json:\n  " + "\n  ".join(stale)
    )


def _gate(gates, key):
    """The gate with the given line-stable ``Gate.key`` (or ``None``)."""
    return next((g for g in gates if g.key == key), None)


# Gates whose every static arm CPython 3.11 attributes to the *opening* line of
# a multi-line construct (a wrapped ``if (``/``if not (`` condition, a multi-line
# body/list literal, or a ``for`` whose exit jumps through an unevaluated
# annotation), while the tracer records the executed transition from an operand
# or body-element line.  Both logical arms are exercised by the corpus, so once
# the executed arcs are normalized to logical lines the gate is fully covered,
# not a gap.  These keep the collapsed opening-line view because their opening
# line *does* carry branch bytecode in 3.11; a wrapped ``and``/``or`` whose
# opening line carries none is instead expanded to operand gates (covered by
# ``test_phantom_boolean_gate_expands_to_operand_arms``).
PHANTOM_MULTILINE_GATES = [
    "tb_handlers.py::if not (::#1",  # wrapped condition fall-through
    "tb_handlers.py::if not (::#2",
    "tb_handlers.py::if not (::#3",
    "tb_handlers.py::if not (::#4",
    "corners.py::if i > 1:::#1",  # single-line ``if`` with multi-line ternary body
    "corners.py::if i < len(points) - 2:::#1",
    "common.py::for edge in graph.edges:::#1",  # exit through unevaluated annotation
    "context.py::if (::#1",  # multi-line ``and``/``or`` conditions
    "context.py::if (::#2",
    "context.py::if (::#3",
]


@pytest.mark.parametrize("key", PHANTOM_MULTILINE_GATES)
def test_multiline_displaced_gate_is_fully_covered(gates, key):
    """A gate whose arcs are displaced to a multi-line opening line is covered.

    CPython records the executed transition from an operand/body line; coverage's
    ``translate_arcs`` maps it back to the logical first line, so pairing it with
    the static arc shows both arms exercised, not an un-exercised gap.
    """
    gate = _gate(gates, key)
    assert gate is not None, f"no gate keyed {key!r}"
    uncovered = [a.dst_line for a in gate.arms if not a.covered]
    assert gate.fully_covered, (
        f"{key} is a phantom multi-line gate but the matrix reports arm(s) "
        f"->{uncovered} un-exercised"
    )


def test_genuine_dead_arm_not_masked_as_covered(gates):
    """A tautologically-dead branch stays a gap, not silenced as phantom.

    ``context.py`` ``if edge.line_id in line_pos:`` skips back to its enclosing
    ``for`` header when false, but ``line_pos`` is built from the same edges so
    the false arm never fires.  Its dst (the loop header) is reached every
    iteration by the loop back-edge, so a dst-reachability heuristic would wrongly
    call it phantom.  Normalizing arcs to logical lines keeps the distinction: the
    false transition has no physical arc to translate, so the gate stays a gap.
    """
    gate = _gate(gates, "context.py::if edge.line_id in line_pos:::#1")
    assert gate is not None, "tautological-membership gate not found"
    assert not gate.fully_covered, (
        "the tautologically-dead false arm is now reported covered; arc "
        "normalization must not merge a never-taken branch onto a live one"
    )


# Operand-level gates that exist only once a phantom multi-line ``and``/``or``
# condition (no branch bytecode on its opening line) is re-attributed to its
# operand lines.  Each carries an un-exercised short-circuit arm the collapsed
# opening-line view masked behind a single phantom verdict.
EXPANDED_OPERAND_GATES = [
    "reversal.py::or (sec_id, succ_id) in horizontal_succ_pairs::#1",
    "inter_section_handlers.py::and src_sec is not None::#1",
    "normalize.py::go is not None::#1",
    "offsets.py::sec is None::#1",
]


@pytest.mark.parametrize("key", EXPANDED_OPERAND_GATES)
def test_phantom_boolean_gate_expands_to_operand_arms(gates, key):
    """A phantom wrapped boolean condition surfaces one gate per operand line.

    CPython emits no branch bytecode on the opening ``if (``/``while (`` line of
    a wrapped ``and``/``or``; the short-circuit branches live on the operand
    lines.  The matrix re-attributes the decision there, so an operand whose
    short-circuit no fixture takes is its own gate instead of hiding behind the
    collapsed opening-line arm.
    """
    gate = _gate(gates, key)
    assert gate is not None, (
        f"expected operand-level gate {key!r}; a phantom multi-line boolean "
        "condition must be re-attributed to its operand lines"
    )
    assert not gate.fully_covered, (
        f"{key} should carry an un-exercised operand arm (a short-circuit branch "
        "no corpus fixture takes)"
    )


def test_reversal_fallthrough_gap_not_masked_by_collapsed_gate(gates):
    """The reversal-propagation fall-through is its own gate, not a phantom one.

    A reversed non-TB section whose cross-section successor is neither on the
    same grid row nor a horizontal LEFT/RIGHT port pair correctly does *not*
    propagate reversal, but no corpus topology exercises that path.  It must show
    as the un-exercised final operand of the ``or`` chain, not collapse onto the
    opening ``if (`` where a single verdict would mask it.
    """
    operand = _gate(
        gates, "reversal.py::or (sec_id, succ_id) in horizontal_succ_pairs::#1"
    )
    assert operand is not None and not operand.fully_covered
    collapsed = [
        g.key
        for g in gates
        if g.module == "reversal.py" and g.code == "if (" and not g.fully_covered
    ]
    assert not collapsed, (
        "a collapsed `reversal.py::if (` gate is still reported as a gap; its "
        f"phantom opening-line arms should be expanded to operands: {collapsed}"
    )
