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
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
BASELINE_PATH = PROJECT_ROOT / "tests" / "data" / "routing_gate_coverage_baseline.json"


def _coverage_already_running() -> bool:
    try:
        import coverage
    except ImportError:
        return False
    return coverage.Coverage.current() is not None


@pytest.fixture(scope="module")
def rgc():
    """The coverage script as an importable module, behind the skip guards.

    Skips the whole module when a coverage tracer is already active (it cannot
    be nested) or the interpreter differs from the pinned baseline (the arc
    model is version-specific).
    """
    if _coverage_already_running():
        pytest.skip("cannot nest a Coverage tracer inside an active coverage run")
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

    Rendering the corpus under coverage is the dominant cost, so all assertions
    share a single run.
    """
    return rgc.compute_gate_coverage()


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
