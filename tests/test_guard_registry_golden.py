"""Golden-baseline equivalence oracle for the ``validate=True`` guard suite.

The guard suite changes *which guards raise and what they say*, not pixels, so
a byte-identical render diff cannot detect a dropped, reordered, or
behaviourally altered guard.  This module pins, for every fixture in the
corpus, the exact ordered sequence of ``_guard_*`` invocations (name + phase)
made during a ``validate=True`` layout, plus the terminal raise (type +
message) when one occurs.

The trace is collected with :func:`sys.setprofile`, which observes the real
Python calls regardless of how the guards are dispatched, so it pins the call
sequence independent of the dispatch mechanism.  The sequence is deterministic
across ``PYTHONHASHSEED``, and the raise messages are built from the final,
seed-independent coordinates.

Regenerate the committed baseline (only legitimate when the guard call order
*intentionally* changes) with::

    NF_METRO_REGEN_GUARD_GOLDEN=1 python -m pytest \
        tests/test_guard_registry_golden.py -q

or directly::

    python tests/test_guard_registry_golden.py
"""

from __future__ import annotations

import json
import os
import sys
import warnings
from pathlib import Path
from types import FrameType
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_engine_guards_perf import _discover_fixtures, _layout  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
BASELINE_PATH = Path(__file__).resolve().parent / "data" / "guard_golden_baseline.json"

ALL_FIXTURES = _discover_fixtures()


def _rel(fixture: str) -> str:
    """Repo-relative POSIX key for a fixture (basenames collide across roots)."""
    return Path(fixture).resolve().relative_to(REPO_ROOT).as_posix()


def collect_guard_trace(fixture: str) -> dict[str, Any]:
    """Run a ``validate=True`` layout and record the guard call sequence.

    Returns ``{"trace": ["guard_name|phase", ...], "raised": None | [type,
    message]}``.  ``trace`` is the in-order list of every ``_guard_*`` function
    entered (name and phase joined for a compact, one-per-line baseline); when
    the layout aborts, the offending guard is the final ``trace`` entry and
    ``raised`` carries its exception type and message.
    """
    trace: list[str] = []

    def _profiler(frame: FrameType, event: str, arg: Any):
        if event != "call":
            return
        name = frame.f_code.co_name
        if name.startswith("_guard_"):
            phase = frame.f_locals.get("phase")
            trace.append(f"{name}|{phase}")

    raised: list[str] | None = None
    prev = sys.getprofile()
    sys.setprofile(_profiler)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            _layout(fixture, validate=True)
    except Exception as exc:  # noqa: BLE001 - the raise is the thing under test
        raised = [type(exc).__name__, str(exc)]
    finally:
        sys.setprofile(prev)

    return {"trace": trace, "raised": raised}


def _load_baseline() -> dict[str, Any]:
    return json.loads(BASELINE_PATH.read_text())


def _regenerate() -> dict[str, Any]:
    baseline: dict[str, Any] = {}
    for fixture in ALL_FIXTURES:
        baseline[_rel(fixture)] = collect_guard_trace(fixture)
    BASELINE_PATH.write_text(json.dumps(baseline, indent=2, sort_keys=True) + "\n")
    return baseline


@pytest.mark.skipif(
    not BASELINE_PATH.exists(),
    reason="guard golden baseline not yet generated",
)
@pytest.mark.parametrize("fixture", ALL_FIXTURES, ids=_rel)
def test_guard_trace_matches_golden_baseline(fixture: str) -> None:
    """The live guard call sequence + terminal raise must match the baseline.

    A drop, reorder, or added guard call -- or a change to which guard raises
    or what it says -- changes ``collect_guard_trace`` for some fixture and
    reds this test.  That is the registry-refactor equivalence signal.
    """
    baseline = _load_baseline()
    key = _rel(fixture)
    assert key in baseline, (
        f"{key} absent from the golden baseline; regenerate with "
        f"NF_METRO_REGEN_GUARD_GOLDEN=1"
    )
    live = collect_guard_trace(fixture)
    expected = baseline[key]

    assert live["raised"] == expected["raised"], (
        f"{key}: terminal raise changed\n  expected: {expected['raised']}\n"
        f"  got:      {live['raised']}"
    )
    assert live["trace"] == expected["trace"], (
        f"{key}: guard call sequence changed "
        f"({len(expected['trace'])} -> {len(live['trace'])} calls)"
    )


def test_baseline_covers_every_fixture() -> None:
    """The committed baseline must name exactly the discovered corpus, so a
    new fixture cannot silently escape the oracle."""
    baseline = _load_baseline()
    discovered = {_rel(f) for f in ALL_FIXTURES}
    assert set(baseline) == discovered, (
        "guard golden baseline is out of sync with the fixture corpus; "
        "regenerate with NF_METRO_REGEN_GUARD_GOLDEN=1\n"
        f"  missing from baseline: {sorted(discovered - set(baseline))}\n"
        f"  stale in baseline:     {sorted(set(baseline) - discovered)}"
    )


if __name__ == "__main__" or os.environ.get("NF_METRO_REGEN_GUARD_GOLDEN"):
    _regenerate()
    print(f"wrote {BASELINE_PATH} ({len(ALL_FIXTURES)} fixtures)")
