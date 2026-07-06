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

The baseline is one JSON file per fixture under ``tests/data/guard_golden/``,
mirroring the fixture's own repo-relative path (e.g. the trace for
``examples/topologies/foo.mmd`` lives at
``tests/data/guard_golden/examples/topologies/foo.json``). This means a PR
touching one fixture's layout only ever diffs that fixture's file, so
unrelated concurrent PRs stop colliding on a single monolithic blob. If git
reports a conflict on one of these files, don't hand-merge the JSON:
resolve the code conflict, then regenerate and let the equivalence test
confirm the regenerated trace is correct.

Regenerate the committed baseline (only legitimate when the guard call order
*intentionally* changes) with::

    NF_METRO_REGEN_GUARD_GOLDEN=1 python -m pytest \
        tests/test_guard_registry_golden.py -q

or directly::

    python tests/test_guard_registry_golden.py

CI regenerates and checks this baseline on ``ubuntu-latest`` (x86_64). A
handful of guards sit right at a floating-point threshold and fire
differently on arm64 (e.g. the symmetric-diamond half-pitch check excluded
from this corpus in commit ``feb2ed23``) -- regenerating locally on Apple
Silicon risks committing an arch-specific trace that then fails in CI. If a
regen changes more fixtures than your fix touched, suspect this before
assuming the extra diffs are real.
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
GUARD_GOLDEN_DIR = Path(__file__).resolve().parent / "data" / "guard_golden"

ALL_FIXTURES = _discover_fixtures()


def _rel(fixture: str) -> str:
    """Repo-relative POSIX key for a fixture (basenames collide across roots)."""
    return Path(fixture).resolve().relative_to(REPO_ROOT).as_posix()


def _baseline_path(fixture: str) -> Path:
    """Per-fixture baseline file, mirroring the fixture's own repo layout."""
    return GUARD_GOLDEN_DIR / Path(_rel(fixture)).with_suffix(".json")


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


def _discover_baseline_files() -> set[Path]:
    if not GUARD_GOLDEN_DIR.exists():
        return set()
    return set(GUARD_GOLDEN_DIR.rglob("*.json"))


def _regenerate() -> None:
    keep: set[Path] = set()
    for fixture in ALL_FIXTURES:
        path = _baseline_path(fixture)
        path.parent.mkdir(parents=True, exist_ok=True)
        trace = collect_guard_trace(fixture)
        path.write_text(json.dumps(trace, indent=2, sort_keys=True) + "\n")
        keep.add(path)

    for stale in _discover_baseline_files() - keep:
        stale.unlink()
    for directory in sorted(GUARD_GOLDEN_DIR.rglob("*"), reverse=True):
        if directory.is_dir() and not any(directory.iterdir()):
            directory.rmdir()


@pytest.mark.skipif(
    not GUARD_GOLDEN_DIR.exists(),
    reason="guard golden baseline not yet generated",
)
@pytest.mark.parametrize("fixture", ALL_FIXTURES, ids=_rel)
def test_guard_trace_matches_golden_baseline(fixture: str) -> None:
    """The live guard call sequence + terminal raise must match the baseline.

    A drop, reorder, or added guard call -- or a change to which guard raises
    or what it says -- changes ``collect_guard_trace`` for some fixture and
    reds this test.  That is the registry-refactor equivalence signal.
    """
    key = _rel(fixture)
    path = _baseline_path(fixture)
    assert path.exists(), (
        f"{key} absent from the golden baseline; regenerate with "
        f"NF_METRO_REGEN_GUARD_GOLDEN=1"
    )
    live = collect_guard_trace(fixture)
    expected = json.loads(path.read_text())

    assert live["raised"] == expected["raised"], (
        f"{key}: terminal raise changed\n  expected: {expected['raised']}\n"
        f"  got:      {live['raised']}"
    )
    assert live["trace"] == expected["trace"], (
        f"{key}: guard call sequence changed "
        f"({len(expected['trace'])} -> {len(live['trace'])} calls)"
    )


def test_baseline_covers_every_fixture() -> None:
    """The committed per-fixture baseline files must name exactly the
    discovered corpus, so a new fixture cannot silently escape the oracle."""
    discovered = {_baseline_path(f) for f in ALL_FIXTURES}
    existing = _discover_baseline_files()
    assert existing == discovered, (
        "guard golden baseline is out of sync with the fixture corpus; "
        "regenerate with NF_METRO_REGEN_GUARD_GOLDEN=1\n"
        f"  missing from baseline: {sorted(str(p) for p in discovered - existing)}\n"
        f"  stale in baseline:     {sorted(str(p) for p in existing - discovered)}"
    )


if __name__ == "__main__" or os.environ.get("NF_METRO_REGEN_GUARD_GOLDEN"):
    _regenerate()
    print(f"wrote {GUARD_GOLDEN_DIR} ({len(ALL_FIXTURES)} fixtures)")
